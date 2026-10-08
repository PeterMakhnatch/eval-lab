"""Set every work-tree path to one fixed timestamp (``mtime-normalize@1``).

Image tar-layer mtimes record when each file was last written at build time.
Files touched by the fix commit carry a later mtime than the bulk checkout,
so ``ls -l`` or ``find -newer`` names the fix files without reading any git
history (002402: ``Makefile`` plus four ``numpyro``/``test`` paths at
22:59:13Z against a 22:49 bulk). ``git clean`` does not reset tracked-file
mtimes, and the strip rebuild never rewrites work-tree bytes, so the cluster
survives both earlier transforms.

The inserted setup block runs at the end of setup, just before the ready
sentinel and therefore after the strip rebuild, ``git clean`` and the purge
reinstall. It sets every file and directory under ``$CWD`` (including the
rebuilt ``.git`` and any surviving build or cache leftovers) to
:data:`FIXED_MTIME`. It deletes nothing; removal stays with
``purge-installed-copies@1``. Setup stops if the touch fails or any path
stays newer than the fixed timestamp. Grading (``tests/``), the instruction
and the image are unchanged. The setup is re-embedded in the existing
task.toml healthcheck payload, which is what actually executes.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "mtime-normalize@1"

#: Marker comment identifying the inserted block (also the idempotence guard).
MARKER = "mtime-normalize@1"

#: Package-relative path of the setup script this transform rewrites.
SETUP_REL = "environment/setup/setup.sh"

#: Ready sentinel; the block runs just before it, at the end of setup.
ANCHOR = 'touch "$M/ready"'

#: The one timestamp every work-tree path carries after setup. A fixed
#: constant, not per-task: within a task all mtimes become identical, and
#: across tasks there is no build clock left to compare.
FIXED_MTIME = "2000-01-01 00:00:00 UTC"

#: ``touch -t`` stamp for the same instant. ``-d`` date parsing differs
#: between GNU and BSD touch, while ``-t [[CC]YY]MMDDhhmm[.SS]`` works on
#: both; the container still runs GNU, where both forms agree.
TOUCH_STAMP = "200001010000.00"

#: Normalize every work-tree path, then fail closed when anything stays newer.
#: ``touch -h`` also normalizes symlink mtimes themselves instead of their
#: targets. ``find -newermt`` is strictly newer, so paths exactly at the
#: fixed timestamp pass. Both literals name local-midnight 2000-01-01, the
#: same instant ``touch -t`` sets; the bare ``YYYY-MM-DD`` form is used
#: because some ``find`` date parsers reject the ``UTC`` suffix.
MTIME_BLOCK = """\
# mtime-normalize@1: one fixed timestamp for every work-tree path, so fix-commit mtimes cannot leak.
find "$CWD" -exec touch -h -t 200001010000.00 {} + || fail "mtime-normalize@1 cannot normalize timestamps"
[ -z "$(find "$CWD" -newermt '2000-01-01' -print -quit)" ] || fail "mtime-normalize@1 found paths newer than the fixed timestamp"
"""

def build_setup_sh(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the mtime block. Returns input unchanged when present."""
    if MARKER in parent_setup_sh:
        return parent_setup_sh
    if parent_setup_sh.count(ANCHOR) != 1:
        raise VariantInvalid("setup.sh has no unique ready sentinel; refusing to normalize mtimes")
    return parent_setup_sh.replace(ANCHOR, MTIME_BLOCK.rstrip("\n") + "\n" + ANCHOR, 1)


def _read_parent(parent_dir: Path) -> tuple[str, str, str, str]:
    try:
        task_toml = tomllib.loads((parent_dir / "task.toml").read_text(encoding="utf-8"))
        task_name = task_toml["task"]["name"]
        workdir = task_toml["environment"]["workdir"]
        image = task_toml["environment"]["docker_image"]
    except Exception as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    try:
        setup_sh = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent {SETUP_REL} is missing: {exc}") from exc
    if not task_name or not workdir or not image:
        raise VariantInvalid("parent task.toml names no task, workdir or image")
    return task_name, workdir, image, setup_sh


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Only ``environment/setup/setup.sh`` and the ``task.toml`` healthcheck
    payload change; grading (``tests/``) is byte-identical by construction.
    """
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh = _read_parent(parent)
    new_setup = build_setup_sh(setup_sh)
    if new_setup == setup_sh:
        raise VariantInvalid("parent setup.sh already carries mtime-normalize@1")
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
        "fixed_mtime": FIXED_MTIME,
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_mtime_normalize(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Set every work-tree path to one fixed timestamp at the end of setup "
        "so fix-commit file mtimes cannot name the fix. Fail setup if any "
        "path stays newer; grading is unchanged."
    ),
    created_by: str = "mtime-normalize",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``mtime-normalize@1`` variant of a MiMo task package.

    The parent may be the snapshot package or a strip/purge variant of it;
    the block anchors on the ready sentinel, which all of them retain, so
    the runtime order is always strip, then purge, then normalize.
    """
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


__all__ = [
    "ANCHOR",
    "FIXED_MTIME",
    "MARKER",
    "MTIME_BLOCK",
    "SETUP_REL",
    "TOUCH_STAMP",
    "TRANSFORM_ID",
    "build_changes",
    "build_setup_sh",
    "derive_mtime_normalize",
]
