"""Container-side purge for ``purge-installed-copies@1``.

Embedded into ``setup.sh`` and executed there. Also imported by tests.
Deletes this project's ``build/``, egg-info and site-packages copies.
Does not delete other distributions or the worktree source.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import re
import shutil
import sys
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


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: purge.py CWD remove|verify|link")
    cwd = Path(sys.argv[1]).resolve()
    roots = install_roots(cwd)
    action = sys.argv[2]
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
