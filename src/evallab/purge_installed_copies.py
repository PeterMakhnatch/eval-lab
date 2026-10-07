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


__all__ = [
    "ANCHOR",
    "MARKER",
    "PURGE_PY",
    "SETUP_REL",
    "TRANSFORM_ID",
    "build_setup_sh",
    "derive_purge_installed_copies",
    "shell_block",
]
