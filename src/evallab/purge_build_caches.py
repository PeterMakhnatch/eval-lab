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

``purge-build-caches@3`` adds node gitignored build outputs (``lib/``,
``dist/``, ``build/``, ``out/``): images baked from the fixed tree carry
the fix in compiled output that setup's ``git clean`` spares via
``--exclude``. A dir counts as grader-used when worktree tests import
through it, a test config or the ``test`` script names it, a self-link
resolves through a package entry point inside it, or any tracked source
references it as a runtime asset path (``path.join(__dirname, '..',
'lib', ...)``, ``fromAsset`` — the shape that broke the naive delete on
000047). Grader-unused dirs are deleted; grader-used dirs are deleted and
rebuilt from the base tree via the package ``compile`` script (``build`` as
fallback; compile regenerates output without running the packaged test suite), and
setup fails when there is no build script or the rebuild does not
regenerate them. @3 supersedes @2: it refuses @1/@2 parents.

``purge-build-caches@4`` is @3 with disabled-cache tolerance. On Modal
sandboxes pip's cache is disabled (``python3 -m pip cache dir`` exits
non-zero with ``cache is disabled`` on stderr), so @2/@3's fail-closed
``_pbc_dir`` precondition aborts setup before any agent phase. When pip
reports a disabled cache — or ``PIP_NO_CACHE_DIR`` is nonempty (including
``off``/``false``/``0``, which pip also interprets as disabling its cache
for backwards compatibility) — @4 treats the pip-cache commands as
not-applicable with a logged reason. Existing cache directories at
``$PIP_CACHE_DIR``, ``~/.cache/pip``, ``/root/.cache/pip`` and
``$XDG_CACHE_HOME/pip`` are removed completely and verified absent
fail-closed. The same rule covers any other leg whose tool reports
a disabled cache (``GOCACHE=off`` logs its reason and removes stale
standard-location build caches while retaining the module-cache purge).
@1-@3 outputs are byte-unchanged; @4 supersedes @3
and refuses @1/@2/@3 parents.

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
# When neither cache directory exists there is nothing to purge.
if [ -f "$CWD/go.mod" ]; then
  _pbc_mod=$(sed -n 's/^module[[:space:]][[:space:]]*\\([^[:space:]]*\\).*/\\1/p' "$CWD/go.mod" | head -n 1)
  [ -n "$_pbc_mod" ] || fail "purge-build-caches@2 cannot read the module path in $CWD/go.mod"
  _pbc_base=$(basename "$_pbc_mod")
  _pbc_esc=$(printf '%s' "$_pbc_base" | sed 's/\\([A-Z]\\)/!\\L\\1/g')
  if [ -n "${GOMODCACHE:-}" ]; then _pbc_gomod="$GOMODCACHE"; else _pbc_gomod=$(go env GOMODCACHE 2>/dev/null || fail "purge-build-caches@2 needs the go toolchain or GOMODCACHE for $CWD/go.mod"); fi
  [ -n "$_pbc_gomod" ] || fail "purge-build-caches@2 resolved an empty GOMODCACHE"
  if [ -n "${GOCACHE:-}" ]; then _pbc_gocache="$GOCACHE"; else _pbc_gocache=$(go env GOCACHE 2>/dev/null || true); fi
  [ -n "${_pbc_gocache:-}" ] || _pbc_gocache=""
  [ "$_pbc_gocache" = "off" ] && _pbc_gocache=""
  if [ ! -d "$_pbc_gomod" ] && [ ! -d "$_pbc_gocache" ]; then
    : # no go caches present: nothing to purge
  else
    if [ -d "$_pbc_gomod" ]; then
      find "$_pbc_gomod" -maxdepth 1 \\( -iname "${_pbc_base}@*" -o -iname "${_pbc_esc}@*" \\) -exec rm -rf {} + 2>/dev/null || fail "purge-build-caches@2 cannot purge the go module cache"
      _pbc_gomod_left=$(find "$_pbc_gomod" -maxdepth 1 \\( -iname "${_pbc_base}@*" -o -iname "${_pbc_esc}@*" \\) -print 2>/dev/null | head -n 5)
      [ -z "$_pbc_gomod_left" ] || fail "purge-build-caches@2 left go module cache entries: $_pbc_gomod_left"
    fi
    if [ -n "$_pbc_gocache" ]; then
      rm -rf "$_pbc_gocache" 2>/dev/null || fail "purge-build-caches@2 cannot purge the go build cache"
      [ ! -e "$_pbc_gocache" ] || fail "purge-build-caches@2 left the go build cache: $_pbc_gocache"
    fi
  fi
fi
# Rust: the worktree target dir and this crate's registry copies.
# Registry purge needs resolvable crate names; when no registry directory
# exists there is nothing to purge and names stay unresolved.
if [ -f "$CWD/Cargo.toml" ]; then
  rm -rf "$CWD/target" 2>/dev/null || fail "purge-build-caches@2 cannot remove $CWD/target"
  [ ! -e "$CWD/target" ] || fail "purge-build-caches@2 left $CWD/target"
  _pbc_cargo="${CARGO_HOME:-$HOME/.cargo}"
  _pbc_reg_found=""
  for _pbc_reg in "$_pbc_cargo/registry/cache" "$_pbc_cargo/registry/src" /root/.cargo/registry/cache /root/.cargo/registry/src; do
    [ -d "$_pbc_reg" ] && _pbc_reg_found="yes"
  done
  if [ -n "$_pbc_reg_found" ]; then
    _pbc_crate=$(sed -n '/^\\[package\\]/,/^\\[/s/^name[[:space:]]*=[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$CWD/Cargo.toml" | head -n 1)
    if [ -z "$_pbc_crate" ]; then
      # Virtual workspace root (no [package]): resolve member crate names.
      _pbc_members=$(sed -n '/^members[[:space:]]*=[[:space:]]*\\[/,/\\]/p' "$CWD/Cargo.toml" | grep -o '"[^"]*"' | tr -d '"' || true)
      [ -n "$_pbc_members" ] || fail "purge-build-caches@2 cannot read the crate name or workspace members in $CWD/Cargo.toml"
      for _pbc_m in $_pbc_members; do
        _pbc_mname=$(sed -n '/^\\[package\\]/,/^\\[/s/^name[[:space:]]*=[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$CWD/$_pbc_m/Cargo.toml" 2>/dev/null | head -n 1 || true)
        [ -n "$_pbc_mname" ] || fail "purge-build-caches@2 cannot read the crate name in $CWD/$_pbc_m/Cargo.toml"
        _pbc_crate="$_pbc_crate $_pbc_mname"
      done
    fi
    for _pbc_reg in "$_pbc_cargo/registry/cache" "$_pbc_cargo/registry/src" /root/.cargo/registry/cache /root/.cargo/registry/src; do
      [ -d "$_pbc_reg" ] || continue
      for _pbc_one in $_pbc_crate; do
        find "$_pbc_reg" -maxdepth 2 -iname "${_pbc_one}-*" -exec rm -rf {} + 2>/dev/null || fail "purge-build-caches@2 cannot purge the cargo registry"
        _pbc_reg_left=$(find "$_pbc_reg" -maxdepth 2 -iname "${_pbc_one}-*" -print 2>/dev/null | head -n 5)
        [ -z "$_pbc_reg_left" ] || fail "purge-build-caches@2 left cargo registry entries: $_pbc_reg_left"
      done
    done
  fi
fi
# Java (Maven): this project's artifacts in the local repository.
# When no local repository exists there is nothing to purge.
if [ -f "$CWD/pom.xml" ] && { [ -d "$HOME/.m2/repository" ] || [ -d /root/.m2/repository ]; }; then
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
# When no node download cache exists there is nothing to purge and the
# package name stays unresolved.
if [ -f "$CWD/package.json" ]; then
  _pbc_node_cache=""
  for _pbc_nd in "${npm_config_cache:-$HOME/.npm}/_cacache" /root/.npm/_cacache "${YARN_CACHE_FOLDER:-$HOME/.cache/yarn}" /root/.cache/yarn "$HOME/.pnpm-store" "$HOME/.local/share/pnpm/store" /root/.local/share/pnpm/store; do
    [ -d "$_pbc_nd" ] && _pbc_node_cache="yes"
  done
  if [ -z "$_pbc_node_cache" ] && command -v pnpm >/dev/null 2>&1; then
    _pbc_psd=$(pnpm store path 2>/dev/null || true)
    [ -n "$_pbc_psd" ] && [ -d "$_pbc_psd" ] && _pbc_node_cache="yes"
  fi
  if [ -n "$_pbc_node_cache" ]; then
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
fi

"""
#: Node gitignored build-output handling appended by ``shell_block_v3``.
#: Compiled output (lib/, dist/, build/, out/) baked from the fixed tree
#: carries the fix, and setup's ``git clean`` spares some of these via
#: ``--exclude``. Tests importing from src/ never need them (delete);
#: tests resolving through them need a base-tree rebuild (else fail closed).
NODE_BUILD_BLOCK = """\\
# purge-build-caches@3: node gitignored build outputs (lib/, dist/, build/, out/).
if [ -f "$CWD/package.json" ]; then
  _pbc_bout=""
  for _pbc_d in lib dist build out; do
    if [ -d "$CWD/$_pbc_d" ] && git -C "$CWD" check-ignore -q "$_pbc_d" 2>/dev/null; then
      _pbc_bout="$_pbc_bout $_pbc_d"
    fi
  done
  if [ -n "$_pbc_bout" ]; then
    _pbc_dep=""
    _pbc_tscript=""
    if command -v node >/dev/null 2>&1; then _pbc_tscript=$(node -p "(require('$CWD/package.json').scripts||{}).test||''" 2>/dev/null || true); [ "$_pbc_tscript" = "undefined" ] && _pbc_tscript=""; fi
    if [ -z "$_pbc_tscript" ] && command -v python3 >/dev/null 2>&1; then _pbc_tscript=$(python3 -c "import json;print((json.load(open('$CWD/package.json')).get('scripts') or {}).get('test') or '')" 2>/dev/null || true); fi
    for _pbc_d in $_pbc_bout; do
      if grep -rlE "(from\\s*|require\\()\\s*['\\\"][^'\\\"]*/$_pbc_d/" "$CWD/test" "$CWD/tests" "$CWD/__tests__" "$CWD/spec" 2>/dev/null | head -n 1 | grep -q .; then _pbc_dep="$_pbc_dep $_pbc_d"; fi
      for _pbc_cfg in "$CWD/jest.config.js" "$CWD/jest.config.ts" "$CWD/jest.config.json" "$CWD/vitest.config.js" "$CWD/vitest.config.ts" "$CWD/.mocharc.js" "$CWD/.mocharc.json" "$CWD/.mocharc.yml" "$CWD/karma.conf.js"; do
        [ -f "$_pbc_cfg" ] || continue
        if grep -E -q "['\\"/]$_pbc_d/" "$_pbc_cfg" 2>/dev/null; then _pbc_dep="$_pbc_dep $_pbc_d"; break; fi
      done
      if git -C "$CWD" grep -lE "['\\\"](\\.\\./)?$_pbc_d['\\\"/]" -- '*.ts' '*.tsx' '*.js' '*.jsx' '*.mjs' '*.cjs' ':!package.json' 2>/dev/null | head -n 1 | grep -q .; then _pbc_dep="$_pbc_dep $_pbc_d"; fi
    done
    _pbc_pkg3=""
    if command -v node >/dev/null 2>&1; then _pbc_pkg3=$(node -p "require('$CWD/package.json').name" 2>/dev/null || true); [ "$_pbc_pkg3" = "undefined" ] && _pbc_pkg3=""; fi
    if [ -z "$_pbc_pkg3" ] && command -v python3 >/dev/null 2>&1; then _pbc_pkg3=$(python3 -c "import json;print(json.load(open('$CWD/package.json')).get('name') or '')" 2>/dev/null || true); fi
    if [ -z "$_pbc_pkg3" ]; then _pbc_pkg3=$(sed -n 's/^[[:space:]]*"name"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$CWD/package.json" | head -n 1); fi
    if [ -n "$_pbc_pkg3" ] && [ -L "$CWD/node_modules/$_pbc_pkg3" ]; then
      _pbc_entry=""
      if command -v node >/dev/null 2>&1; then _pbc_entry=$(node -p "const p=require('$CWD/package.json');p.main||p.types||''" 2>/dev/null || true); [ "$_pbc_entry" = "undefined" ] && _pbc_entry=""; fi
      if [ -z "$_pbc_entry" ] && command -v python3 >/dev/null 2>&1; then _pbc_entry=$(python3 -c "import json;p=json.load(open('$CWD/package.json'));print(p.get('main') or p.get('types') or '')" 2>/dev/null || true); fi
      _pbc_first=$(printf '%s' "$_pbc_entry" | cut -d/ -f1)
      for _pbc_d in $_pbc_bout; do
        if [ -n "$_pbc_first" ] && [ "$_pbc_first" = "$_pbc_d" ]; then _pbc_dep="$_pbc_dep $_pbc_d"; fi
      done
    fi
    _pbc_dep=$(printf '%s' "$_pbc_dep" | tr ' ' '\\n' | sort -u | tr '\\n' ' ')
    for _pbc_d in $_pbc_bout; do
      case " $_pbc_dep " in
        *" $_pbc_d "*) continue;;
      esac
      rm -rf "$CWD/$_pbc_d" 2>/dev/null || fail "purge-build-caches@3 cannot remove $CWD/$_pbc_d"
      [ ! -e "$CWD/$_pbc_d" ] || fail "purge-build-caches@3 left $CWD/$_pbc_d"
    done
    _pbc_left_dep=""
    for _pbc_d in $_pbc_bout; do
      case " $_pbc_dep " in
        *" $_pbc_d "*) [ -d "$CWD/$_pbc_d" ] && _pbc_left_dep="$_pbc_left_dep $_pbc_d";;
      esac
    done
    if [ -n "$_pbc_left_dep" ]; then
      _pbc_script=""
      if command -v node >/dev/null 2>&1; then _pbc_script=$(node -p "const s=require('$CWD/package.json').scripts||{};s.compile?'compile':(s.build?'build':'')" 2>/dev/null || true); [ "$_pbc_script" = "undefined" ] && _pbc_script=""; fi
      if [ -z "$_pbc_script" ] && command -v python3 >/dev/null 2>&1; then _pbc_script=$(python3 -c "import json;s=json.load(open('$CWD/package.json')).get('scripts') or {};print('compile' if 'compile' in s else ('build' if 'build' in s else ''))" 2>/dev/null || true); fi
      [ -n "$_pbc_script" ] || fail "purge-build-caches@3 needs a build script for grader-used output:$_pbc_left_dep"
      for _pbc_d in $_pbc_dep; do
        rm -rf "$CWD/$_pbc_d" 2>/dev/null || fail "purge-build-caches@3 cannot remove stale build output $CWD/$_pbc_d"
      done
      if command -v timeout >/dev/null 2>&1; then timeout 900 npm run "$_pbc_script" --prefix "$CWD" >/dev/null 2>&1 || fail "purge-build-caches@3 rebuild failed";
      else (cd "$CWD" && npm run "$_pbc_script" >/dev/null 2>&1) || fail "purge-build-caches@3 rebuild failed"; fi
      for _pbc_d in $_pbc_left_dep; do
        [ -d "$CWD/$_pbc_d" ] || fail "purge-build-caches@3 rebuild did not regenerate $CWD/$_pbc_d"
      done
    fi
  fi
fi

"""

TRANSFORM_ID_V4 = "purge-build-caches@4"
MARKER_V4 = "purge-build-caches@4"

#: Legs whose tool can report a disabled cache (recorded in @4 lineage
#: inputs). Every other leg is directory-gated: with no cache directory
#: present there is nothing to purge and the section stays silent.
DISABLED_TOLERANT_LEGS = (
    "python-pip",
    "go-build",
)

#: Python/pip leg for ``purge-build-caches@4``. The enabled path is the
#: @2/@3 leg verbatim (same ``@2`` messages: inherited text keeps its
#: original id, as ``CACHE_BLOCK`` keeps ``@1`` under @2/@3). The new
#: disabled path fires when pip reports a disabled cache
#: (``python3 -m pip cache dir`` exits non-zero with ``cache is disabled``
#: on stderr — the Modal sandbox shape) or ``PIP_NO_CACHE_DIR`` is nonempty.
#: Pip's option callback disables caching even for false-like values
#: (``off``/``false``/``0``), for backwards compatibility. Cache commands are
#: skipped with a logged reason, but existing on-disk cache directories are
#: removed entirely and verified absent. Filename filtering would leave
#: content-addressed HTTP bodies, and a disabled cache cannot provide
#: dependency wheels to the offline grader anyway.
PIP_LEG_V4 = """\
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
  _pbc_pip_out=$(python3 -m pip cache dir 2>&1 || true)
  _pbc_pip_off=""
  case "$_pbc_pip_out" in
    *"cache is disabled"*) _pbc_pip_off="pip-reported";;
  esac
  if [ -n "${PIP_NO_CACHE_DIR:-}" ]; then
    _pbc_pip_off="PIP_NO_CACHE_DIR=${PIP_NO_CACHE_DIR}"
  fi
  if [ -n "$_pbc_pip_off" ]; then
    echo "purge-build-caches@4: pip cache disabled (${_pbc_pip_off}); skipping pip cache list/remove"
    for _pbc_d in "${PIP_CACHE_DIR:-}" "$HOME/.cache/pip" /root/.cache/pip "${XDG_CACHE_HOME:-$HOME/.cache}/pip"; do
      [ -n "$_pbc_d" ] || continue
      [ -e "$_pbc_d" ] || [ -L "$_pbc_d" ] || continue
      rm -rf -- "$_pbc_d" || fail "purge-build-caches@4 cannot purge on-disk pip cache $_pbc_d"
      if [ -e "$_pbc_d" ] || [ -L "$_pbc_d" ]; then
        fail "purge-build-caches@4 left on-disk pip cache directory: $_pbc_d"
      fi
    done
  else
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
fi
"""

#: Markers bounding the pip leg inside ``LANG_BLOCK`` for the @4 derivation.
#: The Go leg starts immediately after the pip leg's closing ``fi``.
_PIP_LEG_START = "# Python: this project's wheels in the pip download cache.\n"
_GO_LEG_START = "# Go: this module's entries in the module cache"
#: The disabled Go build-cache path logs its reason and removes stale caches.
_GO_OFF_OLD = '  [ "$_pbc_gocache" = "off" ] && _pbc_gocache=""\n'
_GO_OFF_V4 = """\
  if [ "$_pbc_gocache" = "off" ]; then
    echo "purge-build-caches@4: go build cache disabled (GOCACHE=off); skipping go build cache commands"
    for _pbc_d in "$HOME/.cache/go-build" /root/.cache/go-build "${XDG_CACHE_HOME:-$HOME/.cache}/go-build"; do
      [ -e "$_pbc_d" ] || [ -L "$_pbc_d" ] || continue
      rm -rf -- "$_pbc_d" || fail "purge-build-caches@4 cannot purge on-disk go build cache $_pbc_d"
      if [ -e "$_pbc_d" ] || [ -L "$_pbc_d" ]; then
        fail "purge-build-caches@4 left on-disk go build cache directory: $_pbc_d"
      fi
    done
    _pbc_gocache=""
  fi
"""

_pip_start = LANG_BLOCK.find(_PIP_LEG_START)
_go_start = LANG_BLOCK.find(_GO_LEG_START)
if _pip_start < 0 or _go_start <= _pip_start:
    raise RuntimeError("purge-build-caches@4 derivation anchors moved; update LANG_BLOCK_V4")
if (
    "_pbc_dir=$(python3 -m pip cache dir 2>/dev/null || true)"
    not in LANG_BLOCK[_pip_start:_go_start]
):
    raise RuntimeError("purge-build-caches@4 pip-leg anchor moved; update PIP_LEG_V4")
if LANG_BLOCK.count(_GO_OFF_OLD) != 1:
    raise RuntimeError("purge-build-caches@4 go-off anchor moved; update _GO_OFF_V4")
#: @4 language block: @3 text with the pip leg swapped for the
#: disabled-tolerant ``PIP_LEG_V4`` and the ``GOCACHE=off`` stale-cache purge.
#: Everything else (including ``@1``/``@2`` messages in inherited text) is
#: byte-identical to @3.
LANG_BLOCK_V4 = (LANG_BLOCK[:_pip_start] + PIP_LEG_V4 + LANG_BLOCK[_go_start:]).replace(
    _GO_OFF_OLD, _GO_OFF_V4
)


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


TRANSFORM_ID_V3 = "purge-build-caches@3"
MARKER_V3 = "purge-build-caches@3"


def shell_block_v3() -> str:
    """@2 plus the node gitignored build-output handling. Fail closed."""
    return CACHE_BLOCK + LANG_BLOCK + NODE_BUILD_BLOCK


def shell_block_v4() -> str:
    """@3 plus disabled-cache tolerance in the pip leg. Fail closed."""
    return CACHE_BLOCK + LANG_BLOCK_V4 + NODE_BUILD_BLOCK


def build_setup_sh_v4(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the @4 block. Refuses @1/@2/@3/@4 parents and bare setups."""
    if MARKER_V4 in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-build-caches@4")
    if MARKER_V3 in parent_setup_sh:
        raise VariantInvalid(
            "parent setup.sh already carries purge-build-caches@3; @4 supersedes it"
        )
    if MARKER_V2 in parent_setup_sh:
        raise VariantInvalid(
            "parent setup.sh already carries purge-build-caches@2; @4 supersedes it"
        )
    if MARKER in parent_setup_sh:
        raise VariantInvalid(
            "parent setup.sh already carries purge-build-caches@1; @4 supersedes it"
        )
    if ANCHOR not in parent_setup_sh:
        raise VariantInvalid("setup.sh has no write_blocklist call; refusing to purge caches")
    return parent_setup_sh.replace(ANCHOR, "\nwrite_blocklist\n" + shell_block_v4(), 1)


def build_setup_sh_v3(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the @3 block. Refuses @1/@2/@3 parents and bare setups."""
    if MARKER_V3 in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-build-caches@3")
    if MARKER_V2 in parent_setup_sh:
        raise VariantInvalid(
            "parent setup.sh already carries purge-build-caches@2; @3 supersedes it"
        )
    if MARKER in parent_setup_sh:
        raise VariantInvalid(
            "parent setup.sh already carries purge-build-caches@1; @3 supersedes it"
        )
    if ANCHOR not in parent_setup_sh:
        raise VariantInvalid("setup.sh has no write_blocklist call; refusing to purge caches")
    return parent_setup_sh.replace(ANCHOR, "\nwrite_blocklist\n" + shell_block_v3(), 1)


def build_setup_sh_v2(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the @2 block. Refuses @1/@2 parents and bare setups."""
    if MARKER_V2 in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-build-caches@2")
    if MARKER in parent_setup_sh:
        raise VariantInvalid(
            "parent setup.sh already carries purge-build-caches@1; @2 supersedes it"
        )
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


def build_changes_v3(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @3."""
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh = _read_parent(parent)
    new_setup = build_setup_sh_v3(setup_sh)
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
        "build_output_dirs": ["lib", "dist", "build", "out"],
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_purge_build_caches_v3(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Remove regenerable caches (@2: sweep plus project entries in shared "
        "caches) plus node gitignored build outputs (lib/, dist/, build/, "
        "out/): delete when grading tests import from src/, else rebuild "
        "from the base tree and fail setup when neither is safe. "
        "Shared dependency caches stay."
    ),
    created_by: str = "vals-routes-v3",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive ``purge-build-caches@3`` for a MiMo task package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes_v3(parent)
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
        transform=TRANSFORM_ID_V3,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


def build_changes_v4(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @4."""
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh = _read_parent(parent)
    new_setup = build_setup_sh_v4(setup_sh)
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
        "build_output_dirs": ["lib", "dist", "build", "out"],
        "disabled_cache_tolerance": list(DISABLED_TOLERANT_LEGS),
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_purge_build_caches_v4(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "@3 semantics (sweep plus project entries in shared caches plus node "
        "gitignored build outputs) with disabled-cache tolerance: when a "
        "tool reports its cache disabled (pip's cache on Modal sandboxes, "
        "GOCACHE=off), the leg is not-applicable with a logged reason while "
        "any on-disk pip cache at the standard locations is removed and "
        "verified absent fail-closed. Enabled shared dependency caches stay."
    ),
    created_by: str = "cache-v4",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive ``purge-build-caches@4`` for a MiMo task package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes_v4(parent)
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
        transform=TRANSFORM_ID_V4,
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
    "DISABLED_TOLERANT_LEGS",
    "LANG_BLOCK",
    "LANG_BLOCK_V4",
    "LANGUAGE_SECTIONS",
    "MARKER",
    "MARKER_V2",
    "MARKER_V3",
    "MARKER_V4",
    "NODE_BUILD_BLOCK",
    "PIP_LEG_V4",
    "SETUP_REL",
    "TRANSFORM_ID",
    "TRANSFORM_ID_V2",
    "TRANSFORM_ID_V3",
    "TRANSFORM_ID_V4",
    "build_changes",
    "build_changes_v2",
    "build_changes_v3",
    "build_changes_v4",
    "build_setup_sh",
    "build_setup_sh_v2",
    "build_setup_sh_v3",
    "build_setup_sh_v4",
    "derive_purge_build_caches",
    "derive_purge_build_caches_v2",
    "derive_purge_build_caches_v3",
    "derive_purge_build_caches_v4",
    "foreign_pth_leaks",
    "shell_block",
    "shell_block_v2",
    "shell_block_v3",
    "shell_block_v4",
]
