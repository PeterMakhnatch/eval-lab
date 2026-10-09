"""Remove regenerable build/module caches from MiMo code setups (``purge-build-caches@1``).

Vals' rung 5: with git and network gone the agent searches build and module
caches for the reference patch. The image census (``research/experiments/
vals-closure/cache-census.csv``, n=11 Python tasks) shows every image ships
``__pycache__`` (2k-22k dirs), most ship ``.pytest_cache`` and a project
``egg-info``, and two ship ``build/lib``. Setup's ``git clean -fdx`` already
removes the *untracked* worktree caches before the agent starts (verified
post-setup on 002552: zero survivors), so this transform is defense in depth
for the shapes ``git clean`` cannot reach:

* caches *tracked* in git (committed ``.pyc`` / ``.pytest_cache`` survive the
  clean by design);
* caches inside the setup's ``--exclude`` dirs (``node_modules/.cache``,
  ``.vitest``) which ``git clean`` never enters.

Foreign ``.pth`` / ``egg-link`` pointers (site-packages entries naming a
project copy outside the worktree) are *reported* by
:func:`foreign_pth_leaks` for census and validation, not deleted here:
deleting them risks breaking installers the grader needs, and project copies
themselves belong to ``purge-installed-copies@1``.

What it does not touch, and why: dependency directories themselves
(``node_modules``, ``.venv``, ``vendor``, ``target``, ``site-packages``) and
shared download caches (``~/.cache/pip``, ``~/.npm``) stay, because offline
graders compile and test from them (e.g. Go tasks build from the module
cache; 1.2.0's own setup adds only ``node_modules/.cache`` + ``.vitest``).
Deleting the project's own *copies* stays in ``purge-installed-copies@1``.

The inserted setup block runs after ``write_blocklist`` (same anchor as
``purge-installed-copies@1``, ordered after it) and before the mtime block,
which anchors on the ready sentinel. It fails closed: any surviving listed
cache stops setup before the ready sentinel is written.

``purge-build-caches@2`` is the language-aware superset: the @1 sweep above
plus this project's own entries in shared caches (pip cache wheels of the
project, GOMODCACHE/GOCACHE entries of the project's module, the worktree
cargo ``target/`` plus registry copies of the crate, ``~/.m2`` artifacts of
the project, the Gradle project ``.gradle/`` dir, and npm/yarn/pnpm cache
entries of the package). Each language section fires only when its project
files are present, removes only the project's entries (shared dependency
caches stay: offline graders need them), and fails setup when a listed entry
survives. pnpm's content-addressed store has no per-package eviction, so a
reference there fails closed for manual triage. @2 supersedes @1: it refuses
a parent that already carries @1.

Grading (``tests/``), the instruction and the image are unchanged. The setup
is re-embedded in the task.toml healthcheck payload, which is what executes.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "purge-build-caches@1"
MARKER = "purge-build-caches@1"
SETUP_REL = "environment/setup/setup.sh"
#: Same anchor as purge-installed-copies: this block is appended after the
#: ``write_blocklist`` call, so it runs after the installed-copy purge and
#: before the mtime-normalize block at the ready sentinel.
ANCHOR = "\nwrite_blocklist\n"

#: Worktree cache names deleted wherever they appear under $CWD (outside
#: .git). All are regenerable: Python/pytest/mypy/ruff/hypothesis/tox/nox
#: caches, coverage output, and JS build caches. Dependency dirs and project
#: copies (build/, egg-info) are intentionally absent here.
CACHE_NAMES = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    ".nox",
    "htmlcov",
    ".vitest",
)

#: File-name globs deleted under $CWD (outside .git).
CACHE_GLOBS = (
    "*.pyc",
    "*.pyo",
    ".coverage*",
)

CACHE_BLOCK = """\
# purge-build-caches@1: remove regenerable caches that can carry the fixed
# tree. Dependency dirs (.venv, node_modules, site-packages) and project
# copies (build/, egg-info) stay: offline graders need the former, and the
# latter belong to purge-installed-copies@1.
_cache_names="__pycache__ .pytest_cache .mypy_cache .ruff_cache .hypothesis .tox .nox htmlcov .vitest"
for _n in $_cache_names; do
  find "$CWD" -path "$CWD/.git" -prune -o -name "$_n" -exec rm -rf {} + 2>/dev/null || fail "purge-build-caches@1 cannot remove $_n"
done
find "$CWD/node_modules/.cache" "$CWD/node_modules/.vitest" -maxdepth 0 -exec rm -rf {} + 2>/dev/null; true
find "$CWD" -path "$CWD/.git" -prune -o \\( -name "*.pyc" -o -name "*.pyo" -o -name ".coverage*" \\) -exec rm -f {} + 2>/dev/null || fail "purge-build-caches@1 cannot remove cache files"
_cache_left=$(find "$CWD" -path "$CWD/.git" -prune -o \\( -name "__pycache__" -o -name ".pytest_cache" -o -name "*.pyc" \\) -print 2>/dev/null | head -n 5)
[ -z "$_cache_left" ] || fail "purge-build-caches@1 left caches: $_cache_left"

"""
#: Project-cache purge appended after the @1 sweep by ``shell_block_v2``.
#: Each language section fires only when its project files are present under
#: $CWD, removes only this project's entries from shared caches, and fails
#: setup when a listed entry survives. Env overrides (GOMODCACHE, GOCACHE,
#: CARGO_HOME, PIP_CACHE_DIR, npm_config_cache, YARN_CACHE_FOLDER,
#: PNPM_STORE_PATH) are respected so setups with relocated caches purge the
#: right directories.
LANG_BLOCK = """\\
# purge-build-caches@2: language-aware project-cache purge (runs after @1).
# Python: this project's wheels in the pip download cache.
if [ -f "$CWD/pyproject.toml" ] || [ -f "$CWD/setup.py" ] || [ -f "$CWD/setup.cfg" ]; then
  _pbc_py=$(python3 - "$CWD" 2>/dev/null <<'PY' || true
import configparser, os, re, sys
root = sys.argv[1]
name = None
try:
    text = open(os.path.join(root, "pyproject.toml"), errors="replace").read()
    m = re.search(r"(?ms)^\\[project\\][^\\[]*?^name\\s*=\\s*[\\"']([^\\"']+)", text)
    if m:
        name = m.group(1)
except OSError:
    pass
if name is None:
    try:
        p = configparser.ConfigParser()
        p.read(os.path.join(root, "setup.cfg"))
        name = p.get("metadata", "name", fallback=None)
    except Exception:
        pass
if name is None:
    try:
        text = open(os.path.join(root, "setup.py"), errors="replace").read()
        m = re.search(r"name\\s*=\\s*[\\"']([^\\"']+)", text)
        if m:
            name = m.group(1)
    except OSError:
        pass
if name:
    sys.stdout.write(name.strip())
PY
)
  _pbc_dir=$(python3 -m pip cache dir 2>/dev/null || true)
  [ -n "$_pbc_dir" ] || fail "purge-build-caches@2 needs python3 with pip>=20.1 for the pip cache purge"
  if [ -d "$_pbc_dir" ]; then
    [ -n "$_pbc_py" ] || fail "purge-build-caches@2 cannot identify the Python project name in $CWD"
    _pbc_have=$(python3 -m pip cache list 2>/dev/null | grep -i -F "$_pbc_py" | head -n 5 || true)
    if [ -n "$_pbc_have" ]; then
      _pbc_pat=$(printf '%s' "$_pbc_py" | tr '[:upper:]' '[:lower:]' | tr '_.-' '---')
      python3 -m pip cache remove "${_pbc_py}-*" >/dev/null 2>&1 || true
      python3 -m pip cache remove "${_pbc_pat}-*" >/dev/null 2>&1 || true
      _pbc_left=$(python3 -m pip cache list 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr '_.-' '---' | grep -F "$_pbc_pat" | head -n 5 || true)
      [ -z "$_pbc_left" ] || fail "purge-build-caches@2 left pip cache entries: $_pbc_left"
    fi
  fi
fi
# Go: this module's entries in the module cache, plus the build cache.
if [ -f "$CWD/go.mod" ]; then
  _pbc_mod=$(sed -n 's/^module[[:space:]][[:space:]]*\\([^[:space:]]*\\).*/\\1/p' "$CWD/go.mod" | head -n 1)
  [ -n "$_pbc_mod" ] || fail "purge-build-caches@2 cannot read the module path in $CWD/go.mod"
  _pbc_base=$(basename "$_pbc_mod")
  _pbc_esc=$(printf '%s' "$_pbc_base" | sed 's/\\([A-Z]\\)/!\\L\\1/g')
  if [ -n "${GOMODCACHE:-}" ]; then _pbc_gomod="$GOMODCACHE"; else _pbc_gomod=$(go env GOMODCACHE 2>/dev/null || fail "purge-build-caches@2 needs the go toolchain or GOMODCACHE for $CWD/go.mod"); fi
  [ -n "$_pbc_gomod" ] || fail "purge-build-caches@2 resolved an empty GOMODCACHE"
  find "$_pbc_gomod" -maxdepth 1 \\( -iname "${_pbc_base}@*" -o -iname "${_pbc_esc}@*" \\) -exec rm -rf {} + 2>/dev/null || fail "purge-build-caches@2 cannot purge the go module cache"
  _pbc_gomod_left=$(find "$_pbc_gomod" -maxdepth 1 \\( -iname "${_pbc_base}@*" -o -iname "${_pbc_esc}@*" \\) -print 2>/dev/null | head -n 5)
  [ -z "$_pbc_gomod_left" ] || fail "purge-build-caches@2 left go module cache entries: $_pbc_gomod_left"
  if [ -n "${GOCACHE:-}" ]; then _pbc_gocache="$GOCACHE"; else _pbc_gocache=$(go env GOCACHE 2>/dev/null || true); fi
  if [ -n "${_pbc_gocache:-}" ] && [ "$_pbc_gocache" != "off" ]; then
    rm -rf "$_pbc_gocache" 2>/dev/null || fail "purge-build-caches@2 cannot purge the go build cache"
    [ ! -e "$_pbc_gocache" ] || fail "purge-build-caches@2 left the go build cache: $_pbc_gocache"
  fi
fi
# Rust: the worktree target dir and this crate's registry copies.
if [ -f "$CWD/Cargo.toml" ]; then
  _pbc_crate=$(sed -n '/^\\[package\\]/,/^\\[/s/^name[[:space:]]*=[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$CWD/Cargo.toml" | head -n 1)
  [ -n "$_pbc_crate" ] || fail "purge-build-caches@2 cannot read the crate name in $CWD/Cargo.toml"
  rm -rf "$CWD/target" 2>/dev/null || fail "purge-build-caches@2 cannot remove $CWD/target"
  [ ! -e "$CWD/target" ] || fail "purge-build-caches@2 left $CWD/target"
  _pbc_cargo="${CARGO_HOME:-$HOME/.cargo}"
  for _pbc_reg in "$_pbc_cargo/registry/cache" "$_pbc_cargo/registry/src" /root/.cargo/registry/cache /root/.cargo/registry/src; do
    [ -d "$_pbc_reg" ] || continue
    find "$_pbc_reg" -maxdepth 2 -iname "${_pbc_crate}-*" -exec rm -rf {} + 2>/dev/null || fail "purge-build-caches@2 cannot purge the cargo registry"
    _pbc_reg_left=$(find "$_pbc_reg" -maxdepth 2 -iname "${_pbc_crate}-*" -print 2>/dev/null | head -n 5)
    [ -z "$_pbc_reg_left" ] || fail "purge-build-caches@2 left cargo registry entries: $_pbc_reg_left"
  done
fi
# Java (Maven): this project's artifacts in the local repository.
if [ -f "$CWD/pom.xml" ]; then
  _pbc_ga=$(python3 - "$CWD/pom.xml" 2>/dev/null <<'PY' || true
import sys, xml.etree.ElementTree as ET
try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception:
    sys.exit(1)
ns = "{" + root.tag[1:].split("}")[0] + "}" if root.tag.startswith("{") else ""
def direct(tag):
    el = root.find(ns + tag)
    return el.text.strip() if el is not None and el.text else None
g = direct("groupId")
a = direct("artifactId")
if g is None:
    par = root.find(ns + "parent")
    if par is not None:
        gel = par.find(ns + "groupId")
        g = gel.text.strip() if gel is not None and gel.text else None
if a:
    sys.stdout.write((g or "") + ":" + a)
PY
)
  [ -n "$_pbc_ga" ] || fail "purge-build-caches@2 cannot read groupId/artifactId in $CWD/pom.xml"
  _pbc_gpath=$(printf '%s' "$_pbc_ga" | cut -d: -f1 | tr '.' '/')
  _pbc_art=$(printf '%s' "$_pbc_ga" | cut -d: -f2)
  [ -n "$_pbc_art" ] || fail "purge-build-caches@2 cannot read artifactId in $CWD/pom.xml"
  for _pbc_m2 in "$HOME/.m2/repository" /root/.m2/repository; do
    [ -d "$_pbc_m2/$_pbc_gpath/$_pbc_art" ] || continue
    rm -rf "$_pbc_m2/$_pbc_gpath/$_pbc_art" 2>/dev/null || fail "purge-build-caches@2 cannot purge $_pbc_m2/$_pbc_gpath/$_pbc_art"
    [ ! -e "$_pbc_m2/$_pbc_gpath/$_pbc_art" ] || fail "purge-build-caches@2 left maven artifacts: $_pbc_m2/$_pbc_gpath/$_pbc_art"
  done
fi
# Java (Gradle): the project-local .gradle dir (dependencies in ~/.gradle/caches stay).
if [ -f "$CWD/build.gradle" ] || [ -f "$CWD/build.gradle.kts" ] || [ -f "$CWD/settings.gradle" ] || [ -f "$CWD/settings.gradle.kts" ]; then
  rm -rf "$CWD/.gradle" 2>/dev/null || fail "purge-build-caches@2 cannot remove $CWD/.gradle"
  [ ! -e "$CWD/.gradle" ] || fail "purge-build-caches@2 left $CWD/.gradle"
fi
# Node: this package's tarballs in the shared download caches.
if [ -f "$CWD/package.json" ]; then
  _pbc_pkg=""
  if command -v node >/dev/null 2>&1; then _pbc_pkg=$(node -p "require('$CWD/package.json').name" 2>/dev/null || true); [ "$_pbc_pkg" = "undefined" ] && _pbc_pkg=""; fi
  if [ -z "$_pbc_pkg" ] && command -v python3 >/dev/null 2>&1; then _pbc_pkg=$(python3 -c "import json;print(json.load(open('$CWD/package.json')).get('name') or '')" 2>/dev/null || true); fi
  if [ -z "$_pbc_pkg" ]; then _pbc_pkg=$(sed -n 's/^[[:space:]]*"name"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$CWD/package.json" | head -n 1); fi
  [ -n "$_pbc_pkg" ] || fail "purge-build-caches@2 cannot read the package name in $CWD/package.json"
  _pbc_base=$(basename "$_pbc_pkg")
  _pbc_npm="${npm_config_cache:-$HOME/.npm}"
  for _pbc_npm_root in "$_pbc_npm" /root/.npm; do
    [ -d "$_pbc_npm_root/_cacache/index-v5" ] || continue
    if grep -rlF -e "$_pbc_pkg" -e "/$_pbc_base/-/" "$_pbc_npm_root/_cacache/index-v5" >/dev/null 2>&1; then
      rm -rf "$_pbc_npm_root/_cacache" 2>/dev/null || fail "purge-build-caches@2 cannot purge the npm cache"
      [ ! -e "$_pbc_npm_root/_cacache" ] || fail "purge-build-caches@2 left the npm cache: $_pbc_npm_root/_cacache"
    fi
  done
  if [ -d "${YARN_CACHE_FOLDER:-$HOME/.cache/yarn}" ] || [ -d /root/.cache/yarn ]; then
    if command -v yarn >/dev/null 2>&1; then
      yarn cache clean "$_pbc_pkg" >/dev/null 2>&1 || true
      _pbc_yarn_left=$(yarn cache list 2>/dev/null | grep -i -F -e "${_pbc_pkg}@" -e "${_pbc_base}@" | head -n 5 || true)
      [ -z "$_pbc_yarn_left" ] || fail "purge-build-caches@2 left yarn cache entries: $_pbc_yarn_left"
    else
      fail "purge-build-caches@2 found a yarn cache but no yarn binary for $_pbc_pkg"
    fi
  fi
  _pbc_pnpm="${PNPM_STORE_PATH:-}"
  if [ -z "$_pbc_pnpm" ] && command -v pnpm >/dev/null 2>&1; then _pbc_pnpm=$(pnpm store path 2>/dev/null || true); fi
  for _pbc_ps in "$_pbc_pnpm" "$HOME/.pnpm-store" "$HOME/.local/share/pnpm/store" /root/.local/share/pnpm/store; do
    [ -n "$_pbc_ps" ] && [ -d "$_pbc_ps" ] || continue
    if grep -rlF "$_pbc_pkg" "$_pbc_ps" >/dev/null 2>&1; then
      fail "purge-build-caches@2 found $_pbc_pkg references in the pnpm store $_pbc_ps with no safe per-package eviction"
    fi
  done
fi

"""


def shell_block() -> str:
    """Setup text appended after ``write_blocklist``. Fail closed."""
    return CACHE_BLOCK


TRANSFORM_ID_V2 = "purge-build-caches@2"
MARKER_V2 = "purge-build-caches@2"

#: Language sections in ``LANG_BLOCK`` (recorded in @2 lineage inputs).
LANGUAGE_SECTIONS = (
    "python-pip",
    "go-mod",
    "go-build",
    "rust-target",
    "rust-registry",
    "maven-m2",
    "gradle-project",
    "npm",
    "yarn",
    "pnpm",
)


def shell_block_v2() -> str:
    """@1 sweep plus the language-aware project-cache purge. Fail closed."""
    return CACHE_BLOCK + LANG_BLOCK


def build_setup_sh_v2(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the @2 block. Refuses @1/@2 parents and bare setups."""
    if MARKER_V2 in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-build-caches@2")
    if MARKER in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-build-caches@1; @2 supersedes it")
    if ANCHOR not in parent_setup_sh:
        raise VariantInvalid("setup.sh has no write_blocklist call; refusing to purge caches")
    return parent_setup_sh.replace(ANCHOR, "\nwrite_blocklist\n" + shell_block_v2(), 1)


def build_setup_sh(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the cache-purge block. Refuses a second insert."""
    if MARKER in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-build-caches@1")
    if ANCHOR not in parent_setup_sh:
        raise VariantInvalid("setup.sh has no write_blocklist call; refusing to purge caches")
    return parent_setup_sh.replace(ANCHOR, "\nwrite_blocklist\n" + shell_block(), 1)


def _read_parent(parent_dir: Path) -> tuple[str, str, str, str]:
    try:
        text = (parent_dir / "task.toml").read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    environment = data.get("environment") if isinstance(data, dict) else None
    if not isinstance(environment, dict):
        raise VariantInvalid("parent task.toml has no [environment]")
    workdir = environment.get("workdir")
    image = environment.get("docker_image")
    task_raw = data.get("task")
    task = task_raw if isinstance(task_raw, dict) else {}
    task_name = task.get("name")
    if not isinstance(workdir, str) or not isinstance(image, str) or not isinstance(task_name, str):
        raise VariantInvalid("parent task.toml is missing name, workdir or docker_image")
    setup_path = parent_dir / SETUP_REL
    if not setup_path.is_file():
        raise VariantInvalid(f"parent has no {SETUP_REL}")
    return task_name, workdir, image, setup_path.read_text(encoding="utf-8")


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs."""
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh = _read_parent(parent)
    new_setup = build_setup_sh(setup_sh)
    new_blob = pack_setup(parent / "environment" / "setup", setup_sh=new_setup.encode("utf-8"))
    parent_toml = (parent / "task.toml").read_text(encoding="utf-8")
    changes: dict[str, bytes | None] = {
        SETUP_REL: new_setup.encode("utf-8"),
        "task.toml": render_task_toml(parent_toml, new_blob=new_blob).encode("utf-8"),
    }
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "workdir": workdir,
        "docker_image": image,
        "cache_names": list(CACHE_NAMES),
        "cache_globs": list(CACHE_GLOBS),
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_purge_build_caches(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Remove regenerable build/module caches (pycache, pytest/mypy/ruff "
        "caches, node .cache/.vitest) that can carry the fixed tree, and fail "
        "setup if any survive. Dependency dirs and project copies stay."
    ),
    created_by: str = "vals-closure",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive ``purge-build-caches@1`` for a MiMo task package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes(parent)
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


def build_changes_v2(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @2."""
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh = _read_parent(parent)
    new_setup = build_setup_sh_v2(setup_sh)
    new_blob = pack_setup(parent / "environment" / "setup", setup_sh=new_setup.encode("utf-8"))
    parent_toml = (parent / "task.toml").read_text(encoding="utf-8")
    changes: dict[str, bytes | None] = {
        SETUP_REL: new_setup.encode("utf-8"),
        "task.toml": render_task_toml(parent_toml, new_blob=new_blob).encode("utf-8"),
    }
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "workdir": workdir,
        "docker_image": image,
        "cache_names": list(CACHE_NAMES),
        "cache_globs": list(CACHE_GLOBS),
        "language_sections": list(LANGUAGE_SECTIONS),
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_purge_build_caches_v2(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Remove regenerable caches (@1 sweep) plus this project's own entries "
        "in shared caches (pip wheels, go module/build cache, cargo target and "
        "registry copies, maven artifacts, gradle project cache, npm/yarn/pnpm "
        "package entries), and fail setup if any survive. Shared dependency "
        "caches stay."
    ),
    created_by: str = "vals-routes-v2",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive ``purge-build-caches@2`` for a MiMo task package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes_v2(parent)
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID_V2,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


def foreign_pth_leaks(cwd: Path | str, roots: list[Path | str]) -> list[str]:
    """Project-external ``.pth``/``egg-link`` targets (test seam, no I/O beyond reads).

    Returns ``["<pointer>:<target>", ...]`` for pointers naming a path
    outside ``cwd``. Mirrors the container-side check without executing setup.
    """
    base = Path(cwd).resolve()
    leaks: list[str] = []
    for root in [Path(r).resolve() for r in roots]:
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.suffix == ".pth" and child.is_file():
                try:
                    targets = child.read_text(errors="replace").split()
                except OSError:
                    continue
                for target in targets:
                    if target.startswith(("import ", "#")) or not target.startswith("/"):
                        continue
                    dest = Path(target).resolve()
                    if dest != base and base not in dest.parents:
                        leaks.append(f"{child}:{target}")
            elif child.name.endswith(".egg-link") and child.is_file():
                try:
                    target = child.read_text(errors="replace").split()[0]
                except (OSError, IndexError):
                    continue
                dest = Path(target).resolve()
                if dest != base and base not in dest.parents:
                    leaks.append(f"{child}:{target}")
    return leaks


__all__ = [
    "ANCHOR",
    "CACHE_BLOCK",
    "CACHE_GLOBS",
    "CACHE_NAMES",
    "LANG_BLOCK",
    "LANGUAGE_SECTIONS",
    "MARKER",
    "MARKER_V2",
    "SETUP_REL",
    "TRANSFORM_ID",
    "TRANSFORM_ID_V2",
    "build_changes",
    "build_changes_v2",
    "build_setup_sh",
    "build_setup_sh_v2",
    "derive_purge_build_caches",
    "derive_purge_build_caches_v2",
    "foreign_pth_leaks",
    "shell_block",
    "shell_block_v2",
]
