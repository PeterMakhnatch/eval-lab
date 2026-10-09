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


def shell_block() -> str:
    """Setup text appended after ``write_blocklist``. Fail closed."""
    return CACHE_BLOCK


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
    "MARKER",
    "SETUP_REL",
    "TRANSFORM_ID",
    "build_changes",
    "build_setup_sh",
    "derive_purge_build_caches",
    "foreign_pth_leaks",
    "shell_block",
]
