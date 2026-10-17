"""Container-side purge for ``purge-installed-copies@1`` and ``@2``.

Embedded into ``setup.sh`` and executed there. Also imported by tests.
``@1`` deletes this project's ``build/``, egg-info and site-packages copies.
``@2`` neutralizes copies of the project's tracked sources in place
(BASE bytes overwrite, fix-added files deleted, adjacent pyc dropped) so
installs keep working. Neither touches other distributions or worktree source.
"""

from __future__ import annotations

import ast
import configparser
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _section_value(text: str, header: str, key: str) -> str | None:
    in_section = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1].strip() == header
            continue
        if not in_section or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip().strip("\"'")
        if value:
            return value
    return None


def project_name(cwd: Path) -> str:
    pyproject = cwd / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        for header in ("project", "tool.poetry"):
            found = _section_value(text, header, "name")
            if found:
                return found
    setup_cfg = cwd / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser()
        parser.read(setup_cfg, encoding="utf-8")
        if parser.has_option("metadata", "name"):
            found = parser.get("metadata", "name").strip()
            if found:
                return found
    setup_py = cwd / "setup.py"
    if setup_py.is_file():
        match = re.search(
            r"""name\s*=\s*['"]([^'"]+)['"]""",
            setup_py.read_text(encoding="utf-8", errors="replace"),
        )
        if match:
            return match.group(1)
    raise SystemExit("purge-installed-copies@1: cannot identify the project name")


def import_name(project: str) -> str:
    return project.replace("-", "_")


def is_project_metadata(dirname: str, project: str) -> bool:
    base = dirname
    for suffix in (".dist-info", ".egg-info"):
        if dirname.endswith(suffix):
            base = dirname[: -len(suffix)]
            break
    else:
        return False
    name_part = re.split(r"-(\d)", base, maxsplit=1)[0]
    return normalize(name_part) == normalize(project)


def _under_worktree(path: Path, cwd: Path) -> bool:
    try:
        resolved = path.resolve()
        cwd_resolved = cwd.resolve()
    except OSError:
        return False
    return resolved == cwd_resolved or cwd_resolved in resolved.parents


def is_our_editable(path: Path, cwd: Path) -> bool:
    """True for the dist-info pip writes when it editable-installs this tree."""
    direct = path / "direct_url.json"
    if not direct.is_file():
        return False
    try:
        data = json.loads(direct.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (data.get("dir_info") or {}).get("editable") is not True:
        return False
    url = data.get("url") or ""
    if not url.startswith("file:"):
        return False
    return Path(unquote(urlparse(url).path)).resolve() == cwd.resolve()


def install_roots(cwd: Path) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    candidates: list[Path] = []
    for entry in sys.path:
        if entry:
            path = Path(entry)
            if path.name in {"site-packages", "dist-packages"}:
                candidates.append(path)
    for base in (Path("/usr/local/lib"), Path("/usr/lib")):
        if not base.is_dir():
            continue
        candidates.extend(base.glob("python*/site-packages"))
        candidates.extend(base.glob("python*/dist-packages"))
    cwd_resolved = cwd.resolve()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_dir() or resolved in seen:
            continue
        if resolved == cwd_resolved or cwd_resolved in resolved.parents:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def _drop(path: Path, removed: list[str]) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        return
    removed.append(str(path))


def remove_copies(cwd: Path, roots: list[Path]) -> list[str]:
    """Delete this project's build and installed copies. Keep other packages."""
    project = project_name(cwd)
    name = import_name(project)
    sources = [cwd / name, cwd / "src" / name]
    existed = [path for path in sources if path.exists()]
    removed: list[str] = []
    build = cwd / "build"
    if build.exists() or build.is_symlink():
        _drop(build, removed)
    egg_roots = [cwd]
    if (cwd / "src").is_dir():
        egg_roots.append(cwd / "src")
    for root in egg_roots:
        for egg in root.glob("*.egg-info"):
            if is_project_metadata(egg.name, project):
                _drop(egg, removed)
    for root in roots:
        leak = root / name
        if leak.exists() or leak.is_symlink():
            _drop(leak, removed)
        for child in list(root.iterdir()):
            linked = child.name in {f"{name}.egg-link", f"{project}.egg-link"}
            if linked or is_project_metadata(child.name, project):
                _drop(child, removed)
    for path in existed:
        if not path.exists():
            raise SystemExit(f"purge-installed-copies@1 deleted the worktree source {path}")
    return removed


def remaining_leaks(cwd: Path, roots: list[Path]) -> list[str]:
    project = project_name(cwd)
    name = import_name(project)
    leaks: list[str] = []
    if (cwd / "build").exists() or (cwd / "build").is_symlink():
        leaks.append(str(cwd / "build"))
    for root in roots:
        leak = root / name
        if (leak.exists() or leak.is_symlink()) and not _under_worktree(leak, cwd):
            leaks.append(str(leak))
        for child in root.iterdir():
            if is_project_metadata(child.name, project) and not is_our_editable(child, cwd):
                leaks.append(str(child))
    return leaks


def assert_import_under(cwd: Path) -> None:
    project = project_name(cwd)
    name = import_name(project)
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        raise SystemExit(f"purge-installed-copies@1: cannot import {name}")
    origin = Path(spec.origin).resolve()
    if not _under_worktree(origin, cwd):
        raise SystemExit(f"purge-installed-copies@1: {name} imports from {origin}, not {cwd}")


def project_version(cwd: Path) -> str:
    setup_cfg = cwd / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser()
        parser.read(setup_cfg, encoding="utf-8")
        if parser.has_option("metadata", "version"):
            found = parser.get("metadata", "version").strip()
            if found:
                return found
    pyproject = cwd / "pyproject.toml"
    if pyproject.is_file():
        found = _section_value(
            pyproject.read_text(encoding="utf-8", errors="replace"), "project", "version"
        )
        if found:
            return found
    raise SystemExit("purge-installed-copies@1: cannot identify the project version")


def write_editable_metadata(cwd: Path, roots: list[Path]) -> Path:
    """Publish the base tree's name and version. This is not a copy of the fix."""
    if not roots:
        raise SystemExit("purge-installed-copies@1: no site-packages for metadata")
    project = project_name(cwd)
    version = project_version(cwd)
    dest = roots[0] / f"{import_name(project)}-{version}.dist-info"
    if dest.exists() or dest.is_symlink():
        raise SystemExit("purge-installed-copies@1: installed metadata still present")
    dest.mkdir()
    (dest / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {project}\nVersion: {version}\n",
        encoding="utf-8",
    )
    (dest / "direct_url.json").write_text(
        json.dumps({"url": cwd.resolve().as_uri(), "dir_info": {"editable": True}}) + "\n",
        encoding="utf-8",
    )
    return dest


def link_worktree(cwd: Path, roots: list[Path]) -> str:
    """Point site-packages at the worktree source when pip cannot editable-install."""
    project = project_name(cwd)
    name = import_name(project)
    source = cwd / name
    if not (source / "__init__.py").is_file():
        source = cwd / "src" / name
    if not (source / "__init__.py").is_file():
        raise SystemExit("purge-installed-copies@1: no worktree source to link")
    if not roots:
        raise SystemExit("purge-installed-copies@1: no site-packages to link into")
    dest = roots[0] / name
    if dest.exists() or dest.is_symlink():
        raise SystemExit("purge-installed-copies@1: installed copy still present")
    dest.symlink_to(source.resolve())
    write_editable_metadata(cwd, roots)
    return str(dest)


# --------------------------------------------------------------------------- #
# purge-installed-copies@2: neutralize installed copies in place.
#
# The worktree is at BASE when this runs (strip-future-history@1 precedes it
# in the chain), so BASE bytes come from the worktree files listed by
# ``git ls-files``. Copies outside the worktree (site/dist-packages of every
# interpreter found, worktree and stray ``build/lib*``, stray checkouts) are
# overwritten with BASE bytes — or deleted when BASE has no such file — while
# install metadata (dist-info/egg-info) and other distributions stay intact.
# Container code runs on task-image interpreters back to 3.8: stdlib only.
# --------------------------------------------------------------------------- #

V2_ID = "purge-installed-copies@2"
V2_PRESTATE_NAME = "purge-installed-copies-v2-prestate.json"
V2_LOG_NAME = "purge-installed-copies-v2.log"

#: Worktree layouts whose first component is not installed (``src/<pkg>/...``).
_V2_SRC_LAYOUTS = ("src",)
#: Directory names that mark test trees (excluded from the BASE map).
_V2_TEST_DIRS = ("test", "tests")
#: Single-module stems that are build/config scripts, never installed tops.
_V2_NON_MODULE_STEMS = ("setup", "conftest")
#: Extensions of compiled artifacts: untracked copies stay (documented
#: residual — deleting them could break installs; none of the measured
#: carriers are binaries).
_V2_COMPILED_EXTS = (".so", ".pyd", ".dll", ".dylib")
#: Directory suffixes whose trees are install metadata, never copies.
_V2_METADATA_SUFFIXES = (".dist-info", ".egg-info")
#: Top-level directory names that are never installed packages (docs, samples,
#: tooling). A real distribution with one of these names would be missed;
#: none of the measured tasks has one.
_V2_NON_TOP_DIRS = frozenset(
    {
        "doc",
        "docs",
        "example",
        "examples",
        "scripts",
        "tools",
        "benchmark",
        "benchmarks",
        "demo",
        "demos",
        "tutorial",
        "tutorials",
    }
)
#: Substrings marking env-tooling homes (pipx/pip tooling) that @2 never
#: touches: their content is a task-design residual, not a purge target.
_V2_TOOLING_SUBSTRINGS = (
    "/.local/share/pipx/",
    "/.local/share/pypoetry/",
    "/.local/share/uv/",
    "/pipx/",
)
#: Container prefixes swept for venv site roots and stray checkouts.
_V2_SWEEP_PREFIXES = ("/testbed", "/workspace", "/opt", "/root", "/tmp", "/home")
#: Directory names never descended into during sweeps.
_V2_PRUNE_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".cache",
        ".npm",
        ".cargo",
        ".rustup",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".hypothesis",
    }
)


def _v2_log_path(state_dir: Path | str | None = None) -> Path:
    base = Path(state_dir) if state_dir is not None else Path(tempfile.gettempdir())
    return base / V2_LOG_NAME


def _v2_prestate_path(state_dir: Path | str | None = None) -> Path:
    base = Path(state_dir) if state_dir is not None else Path(tempfile.gettempdir())
    return base / V2_PRESTATE_NAME


def v2_ls_files(cwd: Path) -> list[str]:
    """Worktree-relative tracked paths at BASE (fail-closed when git breaks)."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            V2_ID + ": cannot list tracked files: " + proc.stderr.decode("utf-8", "replace").strip()
        )
    return [entry for entry in proc.stdout.decode("utf-8", "replace").split("\0") if entry]


def _v2_installed_rel(tracked: str) -> str:
    """Map a worktree-relative tracked path to its installed-relative path."""
    parts = tracked.split("/")
    if parts[0] in _V2_SRC_LAYOUTS and len(parts) > 1:
        parts = parts[1:]
    parts[0] = parts[0].replace("-", "_")
    return "/".join(parts)


def _v2_is_test_py(installed_rel: str) -> bool:
    """Whether an installed-relative ``.py`` path is a test file."""
    if not installed_rel.endswith(".py"):
        return False
    comps = installed_rel.split("/")
    if any(comp in _V2_TEST_DIRS for comp in comps[:-1]):
        return True
    stem = comps[-1][:-3]
    return (
        stem == "conftest"
        or stem.startswith("test_")
        or stem.endswith("_test")
        or stem.endswith("_tests")
    )


@dataclass(frozen=True)
class V2PackageOwnership:
    """Installed-relative owned trees, shared boundaries and exact BASE files.

    A namespace initializer belongs to BASE, not to every distribution below
    its directory. The nearest tree/namespace boundary decides whether an
    untracked file is ours to delete; exact tracked files always remain ours.
    """

    roots: frozenset[str]
    namespaces: frozenset[str] = frozenset()
    files: frozenset[str] = frozenset()

    @property
    def tops(self) -> set[str]:
        return {rel.split("/", 1)[0] for rel in self.roots | self.files}

    def owns_directory(self, rel: str) -> bool:
        parts = rel.split("/")
        for end in range(len(parts), 0, -1):
            parent = "/".join(parts[:end])
            if parent in self.namespaces:
                return False
            if parent in self.roots:
                return True
        return False


def _v2_namespace_packages(cwd: Path, tracked: list[str]) -> set[str]:
    """Read legacy namespace declarations without executing project setup code."""
    names: set[str] = set()

    def add(values: object) -> None:
        if not isinstance(values, (list, tuple, set)) or any(
            not isinstance(value, str) or not all(part.isidentifier() for part in value.split("."))
            for value in values
        ):
            raise SystemExit(V2_ID + ": cannot determine declared namespace ownership")
        names.update(value.replace(".", "/") for value in values)

    if "setup.cfg" in tracked:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string((cwd / "setup.cfg").read_text(encoding="utf-8"))
            value = parser.get("options", "namespace_packages", fallback="")
        except (OSError, UnicodeError, configparser.Error) as exc:
            raise SystemExit(V2_ID + ": cannot read namespace ownership from setup.cfg") from exc
        add(value.replace(",", " ").split())
    if "setup.py" not in tracked:
        return names
    try:
        text = (cwd / "setup.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemExit(V2_ID + ": cannot read namespace ownership from setup.py") from exc
    if "namespace_packages" not in text:
        return names
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise SystemExit(V2_ID + ": cannot parse namespace ownership from setup.py") from exc
    recognized: set[ast.AST] = set()
    assignments = {
        target.id: (target, node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def literal(node: ast.AST, seen: frozenset[str] = frozenset()) -> object:
        recognized.add(node)
        if isinstance(node, ast.Name) and node.id in assignments and node.id not in seen:
            target, value = assignments[node.id]
            recognized.add(target)
            return literal(value, seen | {node.id})
        try:
            value = ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise SystemExit(V2_ID + ": cannot determine namespace ownership from setup.py") from exc
        recognized.update(ast.walk(node))
        return value

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "namespace_packages":
            recognized.add(node)
            add(literal(node.value))
        elif isinstance(node, ast.Dict):
            for index, key in enumerate(node.keys):
                value = node.values[index]
                if isinstance(key, ast.Constant) and key.value == "namespace_packages":
                    recognized.add(key)
                    add(literal(value))
    for node in ast.walk(tree):
        if node in recognized:
            continue
        # A namespace marker in a subscript, attribute, indirect call or other
        # unsupported form is not evidence that the declaration is absent.
        if (
            (isinstance(node, ast.Constant) and node.value == "namespace_packages")
            or (isinstance(node, ast.Name) and node.id == "namespace_packages")
            or (isinstance(node, ast.Attribute) and node.attr == "namespace_packages")
        ):
            raise SystemExit(V2_ID + ": cannot determine namespace ownership from setup.py")
    return names


def _v2_namespace_init(content: bytes) -> bool:
    """Recognize pkgutil/pkg_resources namespaces, including imported aliases."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        # Python-2-only initializers still occur in BASE. A possible namespace
        # marker narrows ownership even when this interpreter cannot parse it.
        return re.search(rb"\b(declare_namespace|extend_path|__path__)\b", content) is not None
    calls = {"declare_namespace", "extend_path"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"pkgutil", "pkg_resources"}:
            calls.update(alias.asname or alias.name for alias in node.names if alias.name in calls)
    return any(
        (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr in calls)
                or (isinstance(node.func, ast.Name) and node.func.id in calls)
            )
        )
        or (
            isinstance(node, ast.Name)
            and node.id == "__path__"
            and isinstance(node.ctx, ast.Store)
        )
        for node in ast.walk(tree)
    )


def v2_project_tops(
    cwd: Path, tracked: list[str] | None = None
) -> tuple[V2PackageOwnership, set[str]]:
    """Return source-truthful package ownership and single-module tops.

    Regular tracked ``__init__.py`` files establish owned trees. Missing
    initializers (PEP 420), namespace initializers and static setuptools
    ``namespace_packages`` declarations establish shared boundaries instead.
    Tracked files at those boundaries are exact matches, never permission to
    delete sibling distributions. ``src/`` maps to installed-relative paths.
    """
    if tracked is None:
        tracked = v2_ls_files(cwd)
    installed = {_v2_installed_rel(entry): entry for entry in tracked}
    directories: set[str] = set()
    tops: set[str] = set()
    modules: set[str] = set()
    for rel in installed:
        comps = rel.split("/")
        if not rel.endswith(".py") or _v2_is_test_py(rel):
            continue
        if len(comps) == 1:
            stem = comps[0][:-3]
            if stem.isidentifier() and stem not in _V2_NON_MODULE_STEMS:
                modules.add(stem)
        elif comps[0] not in _V2_NON_TOP_DIRS and all(part.isidentifier() for part in comps[:-1]):
            tops.add(comps[0])
            directories.update("/".join(comps[:end]) for end in range(1, len(comps)))
    namespaces = _v2_namespace_packages(cwd, tracked) if directories else set()
    roots: set[str] = set()
    for directory in sorted(directories):
        init = directory + "/__init__.py"
        if init not in installed:
            namespaces.add(directory)
            continue
        content = _v2_read_base(cwd, installed[init])
        if content is None:
            raise SystemExit(V2_ID + ": cannot determine namespace ownership from " + init)
        if directory in namespaces or _v2_namespace_init(content):
            namespaces.add(directory)
        else:
            roots.add(directory)
    files = {
        rel
        for rel in installed
        if "/" in rel
        and rel.split("/", 1)[0] in tops
        and not any(part.endswith(_V2_METADATA_SUFFIXES) for part in rel.split("/"))
    }
    return V2PackageOwnership(frozenset(roots), frozenset(namespaces), frozenset(files)), modules


def _v2_read_base(cwd: Path, tracked: str) -> bytes | None:
    try:
        return (cwd / tracked).read_bytes()
    except OSError:
        return None


def v2_base_map(
    cwd: Path,
    packages: V2PackageOwnership,
    modules: set[str],
    tracked: list[str] | None = None,
) -> dict[str, bytes | None]:
    """Installed-relative path -> BASE bytes (``None`` when unreadable).

    Covers tracked package sources (including shared namespace initializers,
    tests and data), plus single-module tops. The same ownership matcher drives
    collection and verification; shared siblings never become fix-added files.
    """
    if tracked is None:
        tracked = v2_ls_files(cwd)
    base: dict[str, bytes | None] = {}
    for entry in tracked:
        rel = _v2_installed_rel(entry)
        comps = rel.split("/")
        if any(comp.endswith(_V2_METADATA_SUFFIXES) for comp in comps):
            continue
        if v2_match_rel(rel, packages, modules) is not None:
            base[rel] = _v2_read_base(cwd, entry)
    return base


def _v2_is_tooling(path: Path) -> bool:
    text = path.as_posix()
    return any(mark in text for mark in _V2_TOOLING_SUBSTRINGS)


def _v2_interpreters(cwd: Path) -> list[str]:
    """Candidate interpreter binaries whose site roots may hold copies."""
    found: list[str] = []
    seen: set[Path] = set()
    patterns = [
        "/usr/bin/python3*",
        "/usr/local/bin/python3*",
        "/opt/*/bin/python3*",
        str(cwd / ".venv" / "bin" / "python*"),
        "/testbed/.venv/bin/python*",
        "/workspace/*/.venv/bin/python*",
        "/root/.venv*/bin/python*",
    ]
    for pattern in patterns:
        for cand in sorted(Path("/").glob(pattern.lstrip("/"))):
            if not cand.is_file() or cand.name.endswith("config"):
                continue
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(str(cand))
            if len(found) >= 12:
                return found
    return found


def _v2_query_interpreter(python: str) -> list[str]:
    """``sys.path`` entries of another interpreter (empty when unusable)."""
    try:
        proc = subprocess.run(
            [python, "-c", "import sys; sys.stdout.write(chr(10).join(sys.path))"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.decode("utf-8", "replace").splitlines()


def _v2_sweep_site_dirs(prefixes: list[str]) -> list[Path]:
    """Dirs named site/dist-packages under the prefixes (bounded depth)."""
    found: list[Path] = []
    for prefix in prefixes:
        root = Path(prefix)
        if not root.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > 8:
                continue
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for child in children:
                if child.name in ("site-packages", "dist-packages"):
                    found.append(child)
                    continue
                if (
                    child.name in _V2_PRUNE_NAMES
                    or not child.is_dir()
                    or child.is_symlink()
                    or _v2_is_tooling(child)
                ):
                    continue
                if depth < 8:
                    stack.append((child, depth + 1))
    return found


def _v2_find_named(prefixes: list[str], name: str, maxdepth: int, want: str) -> list[Path]:
    """Paths named ``name`` under the prefixes (``want``: ``dir`` or ``file``)."""
    hits: list[Path] = []
    for prefix in prefixes:
        root = Path(prefix)
        if not root.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > maxdepth:
                continue
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for child in children:
                if child.name == name and (
                    (want == "dir" and child.is_dir()) or (want == "file" and child.is_file())
                ):
                    hits.append(child)
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and child.name not in _V2_PRUNE_NAMES
                    and not _v2_is_tooling(child)
                    and depth < maxdepth
                ):
                    stack.append((child, depth + 1))
    return hits


def v2_find_roots(
    cwd: Path,
    packages: V2PackageOwnership,
    modules: set[str],
    base: dict[str, bytes | None] | None = None,
    sys_path: list[str] | None = None,
    query_interpreter: Callable[[str], list[str]] | None = None,
) -> tuple[list[Path], list[Path], list[tuple[Path, str]], list[Path]]:
    """``(site_roots, build_roots, stray_pkgs, stray_mods)`` for the sweep.

    ``stray_pkgs`` are ``(parent, top)`` pairs whose ``parent/top`` holds a
    copy of the project tree; ``stray_mods`` are stray single-module files.
    Env-tooling homes (pipx/pip) are never roots. ``base`` may carry the
    precomputed BASE map (recomputed when omitted).
    """
    if sys_path is None:
        sys_path = list(sys.path)
    if query_interpreter is None:
        query_interpreter = _v2_query_interpreter
    try:
        cwd_resolved = cwd.resolve()
    except OSError as exc:
        raise SystemExit(V2_ID + ": cannot resolve the worktree " + str(cwd)) from exc
    site: list[Path] = []
    seen: set[Path] = set()

    def add_site(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not resolved.is_dir() or _v2_is_tooling(resolved):
            return
        if resolved == cwd_resolved or cwd_resolved in resolved.parents:
            try:
                rel = resolved.relative_to(cwd_resolved)
            except ValueError:
                return
            if not any(part in (".venv", ".pixi") for part in rel.parts):
                return
        seen.add(resolved)
        site.append(resolved)

    for entry in sys_path:
        if entry and Path(entry).name in ("site-packages", "dist-packages"):
            add_site(Path(entry))
    for python in _v2_interpreters(cwd):
        for entry in query_interpreter(python):
            if entry and Path(entry).name in ("site-packages", "dist-packages"):
                add_site(Path(entry))
    for libdir in (Path("/usr/local/lib"), Path("/usr/lib")):
        if libdir.is_dir():
            for cand in list(libdir.glob("python*/site-packages")) + list(
                libdir.glob("python*/dist-packages")
            ):
                add_site(cand)
    for cand in sorted((Path.home() / ".local" / "lib").glob("python*/site-packages")):
        add_site(cand)
    prefixes = list(_V2_SWEEP_PREFIXES)
    for extra in (cwd / ".venv", cwd / ".pixi"):
        if extra.is_dir():
            prefixes.append(extra.as_posix())
    for cand in _v2_sweep_site_dirs(prefixes):
        add_site(cand)
    build: list[Path] = []
    build_dir = cwd / "build"
    if build_dir.is_dir() and not build_dir.is_symlink():
        for cand in sorted(build_dir.glob("lib*")):
            if cand.is_dir() and not cand.is_symlink():
                build.append(cand.resolve())
    site_resolved = set(site) | {path.resolve() for path in build if path.is_dir()}
    if base is None:
        base = v2_base_map(cwd, packages, modules)
    stray_pkgs: list[tuple[Path, str]] = []
    for top in sorted(packages.tops):
        sample = [rel for rel in base if rel.startswith(top + "/")][:50]
        for hit in _v2_find_named(["/testbed", "/workspace"], top, 4, "dir"):
            try:
                resolved = hit.resolve()
            except OSError:
                continue
            if resolved == cwd_resolved or cwd_resolved in resolved.parents:
                continue
            if any(resolved == root or root in resolved.parents for root in site_resolved):
                continue
            parent = hit.parent
            if (hit / "__init__.py").is_file() or any((parent / rel).is_file() for rel in sample):
                stray_pkgs.append((parent, top))
    stray_mods: list[Path] = []
    for mod in sorted(modules):
        for hit in _v2_find_named(["/testbed", "/workspace"], mod + ".py", 2, "file"):
            try:
                resolved = hit.resolve()
            except OSError:
                continue
            if resolved == cwd_resolved or cwd_resolved in resolved.parents:
                continue
            if any(resolved == root or root in resolved.parents for root in site_resolved):
                continue
            stray_mods.append(hit)
    return site, build, stray_pkgs, stray_mods


def _v2_bytecode_source(rel: str) -> str | None:
    """Return the exact source counterpart of legacy or PEP 3147/488 bytecode."""
    if not rel.endswith((".pyc", ".pyo")):
        return None
    if "__pycache__" in rel.split("/"):
        try:
            # Older tagged caches use .pyo; their source layout is identical.
            return importlib.util.source_from_cache(rel[:-1] + "c")
        except (ValueError, NotImplementedError):
            return None
    return rel[:-1]


def v2_match_rel(rel: str, packages: V2PackageOwnership, modules: set[str]) -> str | None:
    """Map only exact BASE files or files inside proven owned package trees.

    Paths remain anchored at the search root: vendored nested copies never
    match. A shared namespace boundary blocks broad matching until a deeper
    owned package begins. Single-module tops still match directly at the root.
    """
    comps = rel.split("/")
    comps[0] = comps[0].replace("-", "_")
    normalized = "/".join(comps)
    source = _v2_bytecode_source(normalized)
    if source is not None and (
        source in packages.files or ("/" not in source and source[:-3] in modules)
    ):
        return normalized
    if "__pycache__" in comps:
        # An owned tree alone does not identify a cache's source. Keep sibling
        # and unrecognized caches unless an exact BASE module proves ownership.
        return None
    if len(comps) >= 2 and (
        normalized in packages.files or packages.owns_directory("/".join(comps[:-1]))
    ):
        return normalized
    if len(comps) == 1 and rel.endswith(".py") and comps[0][:-3] in modules:
        return rel
    return None


def _v2_under_worktree(path: Path, cwd_resolved: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == cwd_resolved or cwd_resolved in resolved.parents


def _v2_walk_root(
    root: Path,
    cwd_resolved: Path,
    packages: V2PackageOwnership,
    modules: set[str],
    add: Callable[[Path, str, str], None],
    only_top: str | None = None,
) -> None:
    """Collect ``(path, tracked, kind)`` matches under one search root."""
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        keep: list[str] = []
        for name in dirnames:
            full = Path(current) / name
            if Path(current).name == "__pycache__" or name.endswith(_V2_METADATA_SUFFIXES):
                continue
            if only_top is not None and Path(current) == root and name != only_top:
                continue
            if name == "__pycache__":
                if not full.is_symlink():
                    keep.append(name)
                continue
            if full.is_symlink():
                if _v2_under_worktree(full, cwd_resolved):
                    continue
                parts = full.relative_to(root).parts
                rel = "/".join((parts[0].replace("-", "_"),) + parts[1:])
                owned = packages.owns_directory(rel)
                shared_below = any(
                    boundary.startswith(rel + "/") for boundary in packages.namespaces
                )
                if owned and not shared_below:
                    add(full, rel + "/", "linkdir")
                elif owned or any(
                    source.startswith(rel + "/") for source in packages.roots | packages.files
                ):
                    raise SystemExit(
                        V2_ID + ": cannot safely neutralize linked shared namespace " + str(full)
                    )
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            full = Path(current) / name
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            if only_top is not None and not rel.startswith(only_top + "/"):
                continue
            tracked = v2_match_rel(rel, packages, modules)
            if tracked is None:
                continue
            if full.is_symlink() and _v2_under_worktree(full, cwd_resolved):
                continue
            add(full, tracked, "file")


def v2_collect_matches(
    cwd: Path,
    packages: V2PackageOwnership,
    modules: set[str],
    site_roots: list[Path],
    build_roots: list[Path],
    stray_pkgs: list[tuple[Path, str]],
    stray_mods: list[Path],
) -> list[tuple[Path, str, str]]:
    """Every ``(path, tracked, kind)`` copy outside the worktree source."""
    matches: list[tuple[Path, str, str]] = []
    seen: set[Path] = set()
    cwd_resolved = cwd.resolve()

    def add(path: Path, tracked: str, kind: str) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        matches.append((path, tracked, kind))

    for root in site_roots + build_roots:
        if root.is_dir():
            _v2_walk_root(root, cwd_resolved, packages, modules, add)
    for parent, top in stray_pkgs:
        if parent.is_dir():
            _v2_walk_root(parent, cwd_resolved, packages, modules, add, only_top=top)
    for modfile in stray_mods:
        tracked = v2_match_rel(modfile.name, packages, modules)
        if tracked is not None and (modfile.is_file() or modfile.is_symlink()):
            if modfile.is_symlink() and _v2_under_worktree(modfile, cwd_resolved):
                continue
            add(modfile, tracked, "file")
    return matches


def _v2_drop(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _v2_drop_adjacent_pyc(path: Path, log: list[str]) -> None:
    """Delete only adjacent bytecode with this exact module as its source."""
    if path.suffix != ".py":
        return
    cache = path.parent / "__pycache__"
    if cache.is_dir() and not cache.is_symlink():
        for child in sorted(cache.iterdir()):
            if _v2_bytecode_source(child.as_posix()) != path.as_posix():
                continue
            try:
                child.unlink()
            except OSError:
                continue
            log.append(V2_ID + " dropped pyc " + child.as_posix())
    for ext in (".pyc", ".pyo"):
        legacy = path.parent / (path.stem + ext)
        if legacy.is_file() and not legacy.is_symlink():
            try:
                legacy.unlink()
            except OSError:
                continue
            log.append(V2_ID + " dropped pyc " + legacy.as_posix())


def _v2_worktree_source(cwd: Path, top: str) -> Path | None:
    for cand in (cwd / top, cwd / "src" / top):
        if cand.is_dir() and not cand.is_symlink():
            return cand
    return None


def v2_neutralize_matches(
    cwd: Path,
    matches: list[tuple[Path, str, str]],
    base: dict[str, bytes | None],
) -> tuple[list[str], int]:
    """Neutralize every match. Returns ``(log lines, changed count)``.

    Tracked copies are overwritten with BASE bytes; fix-added source files
    are deleted; untracked compiled artifacts stay (documented residual).
    Fail-closed when BASE bytes for a tracked copy cannot be read.
    """
    log: list[str] = []
    changed = 0
    for path, tracked, kind in matches:
        if kind == "linkdir":
            top = tracked.rstrip("/")
            _v2_drop(path)
            source = _v2_worktree_source(cwd, top)
            if source is None:
                log.append(V2_ID + " removed linked copy " + path.as_posix())
            else:
                shutil.copytree(source, path, ignore=shutil.ignore_patterns("__pycache__"))
                log.append(V2_ID + " materialized " + path.as_posix() + " from BASE")
            changed += 1
            continue
        content = base.get(tracked)
        if tracked in base and content is None:
            raise SystemExit(V2_ID + ": cannot read BASE bytes for " + tracked)
        if tracked not in base:
            # A preceding source match may already have removed this cache.
            if not path.exists() and not path.is_symlink():
                continue
            if path.suffix in _V2_COMPILED_EXTS and path.is_file():
                log.append(V2_ID + " kept compiled artifact " + path.as_posix())
                continue
            _v2_drop(path)
            log.append(V2_ID + " deleted fix-added file " + path.as_posix())
            changed += 1
            _v2_drop_adjacent_pyc(path, log)
            continue
        assert content is not None
        if path.is_symlink():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        log.append(V2_ID + " neutralized " + path.as_posix() + " to BASE")
        changed += 1
        _v2_drop_adjacent_pyc(path, log)
    return log, changed


def _v2_run_isolated(script: str, timeout: int) -> tuple[int, str, str]:
    """Run ``script`` with the stock interpreter, outside the worktree."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", "probe failed: " + str(exc)[:200]
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def v2_probe_imports(
    tops: list[str], run: Callable[[str], tuple[int, str, str]] | None = None
) -> dict[str, tuple[bool, str]]:
    """Import each top isolated from the worktree. Returns ``(ok, detail)``.

    Existence (``find_spec``) is not enough: base trees like theano carry
    files that never parse on the image interpreter, so only a real import
    proves the installed copy works — and only tops that import *before*
    neutralizing must still import after.
    """

    def default_run(script: str) -> tuple[int, str, str]:
        return _v2_run_isolated(script, 300)

    if run is None:
        run = default_run
    out: dict[str, tuple[bool, str]] = {}
    for top in tops:
        if not top.isidentifier():
            out[top] = (False, "not an importable name")
            continue
        code, _, stderr = run("import " + top)
        if code == 0:
            out[top] = (True, "ok")
        else:
            tail = stderr.strip().splitlines()[-1] if stderr.strip() else "rc=" + str(code)
            out[top] = (False, tail[:200])
    return out


def v2_try_import(
    top: str, run: Callable[[str], tuple[int, str, str]] | None = None
) -> tuple[bool, str]:
    """Import ``top`` isolated from the worktree. Returns ``(ok, detail)``."""
    if not top.isidentifier():
        return False, "not an importable name"

    def default_run(script: str) -> tuple[int, str, str]:
        return _v2_run_isolated(script, 300)

    if run is None:
        run = default_run
    code, _, stderr = run("import " + top)
    if code == 0:
        return True, "ok"
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else "rc=" + str(code)
    return False, tail[:200]


def v2_action_neutralize(
    cwd: Path,
    *,
    tracked: list[str] | None = None,
    roots: tuple[list[Path], list[Path], list[tuple[Path, str]], list[Path]] | None = None,
    sys_path: list[str] | None = None,
    query_interpreter: Callable[[str], list[str]] | None = None,
    probe_run: Callable[[str], tuple[int, str, str]] | None = None,
    state_dir: Path | str | None = None,
) -> None:
    """@2 setup step one: overwrite copies with BASE bytes, record prestate."""
    packages, modules = v2_project_tops(cwd, tracked=tracked)
    tops = sorted(packages.tops | modules)
    log_path = _v2_log_path(state_dir)
    if not tops:
        line = V2_ID + ": no project tops in " + str(cwd) + "; nothing to do"
        print(line)
        log_path.write_text(line + "\n", encoding="utf-8")
        _v2_prestate_path(state_dir).write_text(
            json.dumps({"tops": [], "pre_origins": {}, "changed": 0}) + "\n",
            encoding="utf-8",
        )
        return
    base = v2_base_map(cwd, packages, modules, tracked=tracked)
    if roots is None:
        roots = v2_find_roots(
            cwd,
            packages,
            modules,
            base=base,
            sys_path=sys_path,
            query_interpreter=query_interpreter,
        )
    site_roots, build_roots, stray_pkgs, stray_mods = roots
    matches = v2_collect_matches(
        cwd, packages, modules, site_roots, build_roots, stray_pkgs, stray_mods
    )
    pre = v2_probe_imports(tops, run=probe_run)
    for top in tops:
        ok, detail = pre[top]
        print(V2_ID + ": pre-import " + top + " " + ("ok" if ok else "FAIL (" + detail + ")"))
    log, changed = v2_neutralize_matches(cwd, matches, base)
    lines = [
        V2_ID + ": tops " + ",".join(tops),
        V2_ID
        + ": roots "
        + str(len(site_roots))
        + " site, "
        + str(len(build_roots))
        + " build, "
        + str(len(stray_pkgs))
        + " stray",
        V2_ID + ": matched " + str(len(matches)) + " copies, changed " + str(changed),
    ] + log
    print("\n".join(lines))
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _v2_prestate_path(state_dir).write_text(
        json.dumps(
            {
                "tops": tops,
                "pre_ok": {top: ok for top, (ok, _) in pre.items()},
                "changed": changed,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def v2_action_verify(
    cwd: Path,
    *,
    tracked: list[str] | None = None,
    roots: tuple[list[Path], list[Path], list[tuple[Path, str]], list[Path]] | None = None,
    sys_path: list[str] | None = None,
    query_interpreter: Callable[[str], list[str]] | None = None,
    import_run: Callable[[str], tuple[int, str, str]] | None = None,
    state_dir: Path | str | None = None,
) -> None:
    """@2 setup step two, fail-closed: no non-base copy remains; import holds."""
    packages, modules = v2_project_tops(cwd, tracked=tracked)
    base = v2_base_map(cwd, packages, modules, tracked=tracked)
    if roots is None:
        roots = v2_find_roots(
            cwd,
            packages,
            modules,
            base=base,
            sys_path=sys_path,
            query_interpreter=query_interpreter,
        )
    site_roots, build_roots, stray_pkgs, stray_mods = roots
    matches = v2_collect_matches(
        cwd, packages, modules, site_roots, build_roots, stray_pkgs, stray_mods
    )
    problems: list[str] = []
    for path, rel, kind in matches:
        if kind == "linkdir":
            problems.append("linked copy remains: " + path.as_posix())
            continue
        content = base.get(rel)
        if rel in base:
            if content is None:
                problems.append("unreadable BASE for " + rel)
                continue
            try:
                current = path.read_bytes() if path.is_file() else None
            except OSError:
                current = None
            if current != content:
                problems.append("non-base bytes remain: " + path.as_posix())
        elif path.suffix in _V2_COMPILED_EXTS and path.is_file():
            continue
        else:
            problems.append("untracked copy remains: " + path.as_posix())
    if problems:
        shown = "; ".join(problems[:20])
        if len(problems) > 20:
            shown += " (+" + str(len(problems) - 20) + " more)"
        raise SystemExit(V2_ID + " left non-base copies: " + shown)
    try:
        prestate = json.loads(_v2_prestate_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prestate = {}
    if not isinstance(prestate, dict) or not prestate.get("changed"):
        print(V2_ID + ": no copy neutralized; import check skipped")
        return
    pre_ok = prestate.get("pre_ok") or {}
    if not isinstance(pre_ok, dict):
        raise SystemExit(V2_ID + ": corrupt prestate; refusing to pass")
    for top in sorted(prestate.get("tops") or []):
        if not pre_ok.get(top):
            print(V2_ID + ": " + top + " did not import before; import check skipped")
            continue
        ok, detail = v2_try_import(top, run=import_run)
        if not ok:
            raise SystemExit(
                V2_ID + ": import " + top + " worked before but fails after neutralize: " + detail
            )
        print(V2_ID + ": import " + top + " still succeeds")
    print(V2_ID + ": verified, no non-base copy remains")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: purge.py CWD remove|verify|link|neutralize|verify2")
    cwd = Path(sys.argv[1]).resolve()
    action = sys.argv[2]
    if action == "neutralize":
        v2_action_neutralize(cwd)
        return
    if action == "verify2":
        v2_action_verify(cwd)
        return
    roots = install_roots(cwd)
    if action == "remove":
        print("purge-installed-copies@1 removed", len(remove_copies(cwd, roots)))
        return
    if action == "link":
        print("purge-installed-copies@1 linked", link_worktree(cwd, roots))
        return
    if action == "verify":
        leaks = remaining_leaks(cwd, roots)
        if leaks:
            raise SystemExit("purge-installed-copies@1 left copies: " + ", ".join(leaks))
        assert_import_under(cwd)
        print("purge-installed-copies@1 import under", cwd)
        return
    raise SystemExit("unknown purge action " + action)


if __name__ == "__main__":
    main()
