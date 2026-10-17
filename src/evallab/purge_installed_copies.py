"""Remove installed copies of a task's own project (``purge-installed-copies@1``).

``git clean`` in the MiMo setup excludes ``lib``, so ``build/lib`` survives,
and a copy under ``site-packages`` is outside the worktree entirely. Both can
hold the fix while the worktree is the base (HAR-185: 001269 ``responses``,
002308 ``pre_commit``).

The inserted setup block deletes ``$CWD/build``, the project's own egg-info,
and matching site-packages / dist-packages entries, then reinstalls the base
tree editable and offline (``--no-deps --no-index``). It does not delete other
distributions. Setup stops if the project name cannot be resolved, the
editable install fails, a copy remains, or the import does not resolve under
the worktree. There is no fallback that leaves the copy in place.

Grading (``tests/``), the instruction and the image are unchanged. The setup
is re-embedded in the task.toml healthcheck payload, which is what executes.

``purge-installed-copies@2`` supersedes ``@1`` for all Python tasks: instead
of deleting copies and reinstalling (fail-closed on poetry backends,
unidentifiable projects and vendored copies), it neutralizes copies of the
project's tracked sources in place. Tracked files outside the worktree whose
path starts at the project's own top-level package (or a top-level
single-module file directly under a site/dist-packages/build root) are
overwritten with the worktree's BASE bytes; fix-added files are deleted;
adjacent ``__pycache__`` entries are dropped; install metadata and other
distributions stay. Verification fails setup when a non-base copy remains or
the top-level package no longer imports. No-op when nothing matches.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Any

from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "purge-installed-copies@1"
MARKER = "purge-installed-copies@1"
SETUP_REL = "environment/setup/setup.sh"
#: The call, not the ``write_blocklist()`` definition.
ANCHOR = "\nwrite_blocklist\n"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SCRIPT = Path(__file__).with_name("purge_container.py")
PURGE_PY = _SCRIPT.read_text(encoding="utf-8")
if "def remove_copies" not in PURGE_PY or "def main" not in PURGE_PY:
    raise RuntimeError("purge container script is missing remove_copies or main")
if "def v2_neutralize_matches" not in PURGE_PY or "def v2_action_verify" not in PURGE_PY:
    raise RuntimeError("purge container script is missing the @2 neutralize entry points")

TRANSFORM_ID_V2 = "purge-installed-copies@2"
#: Modern setup helpers use the image's system Python, not its Python 3.6
#: project interpreter. This is exported only in the setup child process.
SETUP_PYTHON_PATH = "/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/bin"
#: First line of the @2 setup block. The bare id is NOT a marker: the
#: embedded container script mentions it in @2 messages, so a carried @1
#: block (same embedded script) would false-positive on a substring check.
MARKER_V2 = "# purge-installed-copies@2: neutralize installed copies of this project's\n"
#: First line of the @1 setup block; the @1 block runs to its third heredoc end.
V1_BLOCK_START = "# purge-installed-copies@1: remove this project's build and installed copies,\n"
_HEREDOC_END = "\nPY\n"


def shell_block() -> str:
    """Setup text inserted before ``write_blocklist``. Fail closed."""
    return (
        "# purge-installed-copies@1: remove this project's build and installed copies,\n"
        "# then reinstall the base tree editable and offline. Other distributions stay.\n"
        "python3 - \"$CWD\" remove <<'PY' || fail \"purge-installed-copies@1 failed\"\n"
        + PURGE_PY
        + "PY\n"
        "if ! python3 -m pip install -e \"$CWD\" --no-deps --no-index --no-build-isolation; then\n"
        "  echo \"purge-installed-copies@1: pip editable install unavailable; linking the worktree source\"\n"
        "  python3 - \"$CWD\" link <<'PY' || fail \"purge-installed-copies@1 editable install failed\"\n"
        + PURGE_PY
        + "PY\n"
        "fi\n"
        "rm -rf \"$CWD/build\" || fail \"purge-installed-copies@1 cannot remove build\"\n"
        "python3 - \"$CWD\" verify <<'PY' || fail \"purge-installed-copies@1 left an installed copy\"\n"
        + PURGE_PY
        + "PY\n"
    )


def build_setup_sh(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the purge block. Refuses a second insert."""
    if MARKER in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-installed-copies@1")
    if ANCHOR not in parent_setup_sh:
        raise VariantInvalid("setup.sh has no write_blocklist call; refusing to purge")
    return parent_setup_sh.replace(ANCHOR, "\n" + shell_block() + "write_blocklist\n", 1)


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


def derive_purge_installed_copies(
    parent_dir: Path | str,
    *,
    repairs_digest: str,
    rationale: str = (
        "Remove build/, project egg-info and site-packages copies of this "
        "project, then reinstall the base tree editable and offline. Fail "
        "setup if a copy remains or the import does not resolve under the worktree."
    ),
    created_by: str = "har194-hardening",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive ``purge-installed-copies@1`` for the package the ledger runs.

    ``repairs_digest`` is that package's digest. The parent may be a strip
    variant of it; the ledger switches to this variant only once it is validated.
    """
    if _DIGEST.fullmatch(repairs_digest) is None:
        raise VariantInvalid(f"repairs_digest is not a sha256 digest: {repairs_digest}")
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
        "repairs_digest": repairs_digest,
        "removed": ["build/", "project-egg-info", "site-packages-copy"],
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
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


def shell_block_v2() -> str:
    """Setup text inserted before ``write_blocklist``. Neutralize, then verify."""
    return (
        MARKER_V2 + "# tracked sources in place (BASE bytes overwrite, fix-added files deleted,\n"
        "# adjacent pyc dropped). Install metadata and other distributions stay.\n"
        'python3 - "$CWD" neutralize <<\'PY\' || fail "purge-installed-copies@2 failed"\n'
        + PURGE_PY
        + "PY\n"
        'python3 - "$CWD" verify2 <<\'PY\' || fail "purge-installed-copies@2 left a non-base copy"\n'
        + PURGE_PY
        + "PY\n"
    )


def _excise_v1_block(parent_setup_sh: str) -> str:
    """Remove the @1 setup block (three heredocs) so @2 supersedes it."""
    start = parent_setup_sh.find(V1_BLOCK_START)
    if start < 0:
        return parent_setup_sh
    end = start
    for _ in range(3):
        end = parent_setup_sh.find(_HEREDOC_END, end + len(V1_BLOCK_START))
        if end < 0:
            raise VariantInvalid("parent @1 block has an unexpected shape; refusing to purge")
        end += len(_HEREDOC_END)
    excised = parent_setup_sh[:start] + parent_setup_sh[end:]
    if V1_BLOCK_START in excised:
        raise VariantInvalid("parent @1 block has an unexpected shape; refusing to purge")
    return excised


def build_setup_sh_v2(parent_setup_sh: str) -> tuple[str, bool]:
    """Parent ``setup.sh`` plus the @2 block; returns ``(setup, superseded_v1)``.

    Refuses a second @2 insert and setups without the ``write_blocklist``
    anchor. A carried @1 block is excised (@2 supersedes it) instead of
    stacking a second purge. Pin helper lookup before any parent setup
    commands; the healthcheck invokes this script in a child shell, so the
    project's test-runner PATH and interpreter are unchanged.
    """
    if MARKER_V2 in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-installed-copies@2")
    if ANCHOR not in parent_setup_sh:
        raise VariantInvalid("setup.sh has no write_blocklist call; refusing to purge")
    superseded = V1_BLOCK_START in parent_setup_sh
    without_v1 = _excise_v1_block(parent_setup_sh)
    setup_path = (
        "# Setup helpers need system Python; do not change the project test runner.\n"
        f"export PATH='{SETUP_PYTHON_PATH}'\n"
    )
    if without_v1.startswith("#!"):
        shebang, _, body = without_v1.partition("\n")
        without_v1 = shebang + "\n" + setup_path + body
    else:
        without_v1 = setup_path + without_v1
    return (
        without_v1.replace(ANCHOR, "\n" + shell_block_v2() + "write_blocklist\n", 1),
        superseded,
    )


def derive_purge_installed_copies_v2(
    parent_dir: Path | str,
    *,
    repairs_digest: str,
    rationale: str = (
        "Neutralize installed copies of this project's tracked sources in "
        "place (BASE bytes overwrite, fix-added files deleted, adjacent pyc "
        "dropped). Fail setup if a non-base copy remains or the top-level "
        "package no longer imports. No-op when nothing matches."
    ),
    created_by: str = "purge-installed-copies-v2",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive ``purge-installed-copies@2`` for any Python package.

    ``repairs_digest`` is the ledger run package digest the chain builds on.
    Unlike @1 this applies to every Python task: the container step no-ops
    when no copy matches, so unidentifiable projects and exotic backends no
    longer fail setup closed.
    """
    if _DIGEST.fullmatch(repairs_digest) is None:
        raise VariantInvalid(f"repairs_digest is not a sha256 digest: {repairs_digest}")
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh = _read_parent(parent)
    new_setup, superseded = build_setup_sh_v2(setup_sh)
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
        "repairs_digest": repairs_digest,
        "removed": ["neutralized-in-place"],
        "superseded_v1": superseded,
        "setup_python_path": SETUP_PYTHON_PATH,
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
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


__all__ = [
    "ANCHOR",
    "MARKER",
    "MARKER_V2",
    "PURGE_PY",
    "SETUP_PYTHON_PATH",
    "SETUP_REL",
    "TRANSFORM_ID",
    "TRANSFORM_ID_V2",
    "V1_BLOCK_START",
    "build_setup_sh",
    "build_setup_sh_v2",
    "derive_purge_installed_copies",
    "derive_purge_installed_copies_v2",
    "shell_block",
    "shell_block_v2",
]
