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
stays newer than the fixed timestamp (``@2`` additionally warms lazily
materialized layers, retries touch+check up to three passes, and stops when
any path's mtime merely differs from the fixed timestamp). Grading
(``tests/``), the instruction
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

#: Second-generation transform: same goal, hardened for lazy layer
#: materialization (Modal ``_ModalDirect``). Recorded in lineage as
#: ``mtime-normalize@2``; ``@1`` records stay valid, ``@2`` supersedes ``@1``
#: (a ``@1`` parent is refused, like the purge ``@2``/``@3`` supersedes).
TRANSFORM_ID_V2 = "mtime-normalize@2"

#: Marker comment identifying the inserted @2 block.
MARKER_V2 = "mtime-normalize@2"

#: Bounded touch+check passes in the @2 block: the FleetCensus Modal repro
#: showed one re-bump after the first touch walk and zero offenders on the
#: second pass, so three passes bound the retry with margin.
MAX_PASSES_V2 = 3

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


#: Materialize, then normalize with bounded retries, then require every mtime
#: to equal the fixed timestamp. The read-only ``stat`` walk forces lazily
#: materialized layers (Modal ``_ModalDirect`` from ``Image.from_registry``)
#: to appear before the first touch; directory mtimes re-bumped by a late
#: materialization are caught by the ``-newermt`` re-check and fixed by the
#: next pass (at most :data:`MAX_PASSES_V2` touches total). The final loop
#: compares every path's epoch against a freshly stamped reference file, so
#: older-than-fixed mtimes fail too — strictly stronger than ``@1``'s
#: newer-only check. The loop is plain POSIX shell with no pipelines, so
#: ``pipefail``/``SIGPIPE`` cannot mask an offender; every fallible step
#: fails closed via ``fail``.
MTIME_BLOCK_V2 = """\
# mtime-normalize@2: one fixed timestamp for every work-tree path, so fix-commit mtimes cannot leak.
find "$CWD" -exec stat {} + >/dev/null || fail "mtime-normalize@2 cannot materialize paths"
_mtime_try=1
while [ "$_mtime_try" -le 3 ]; do
find "$CWD" -exec touch -h -t 200001010000.00 {} + || fail "mtime-normalize@2 cannot normalize timestamps"
[ -n "$(find "$CWD" -newermt '2000-01-01' -print -quit)" ] || break
_mtime_try=$((_mtime_try + 1))
done
[ -z "$(find "$CWD" -newermt '2000-01-01' -print -quit)" ] || fail "mtime-normalize@2 found paths newer than the fixed timestamp"
_mtime_ref=$(mktemp) || fail "mtime-normalize@2 cannot create reference file"
touch -t 200001010000.00 "$_mtime_ref" || fail "mtime-normalize@2 cannot stamp reference file"
_mtime_expected=$(stat -c %Y "$_mtime_ref") || fail "mtime-normalize@2 cannot read reference timestamp"
rm -f "$_mtime_ref"
_mtime_epochs=$(find "$CWD" -exec stat -c %Y {} +) || fail "mtime-normalize@2 cannot read timestamps"
_mtime_bad=0
for _mtime_e in $_mtime_epochs; do
[ "$_mtime_e" = "$_mtime_expected" ] || _mtime_bad=1
done
[ "$_mtime_bad" -eq 0 ] || fail "mtime-normalize@2 found paths with mtime differing from the fixed timestamp"
"""

def build_setup_sh_v2(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the @2 block. Refuses @2 and @1 parents."""
    if MARKER_V2 in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries mtime-normalize@2")
    if MARKER in parent_setup_sh:
        raise VariantInvalid(
            "parent setup.sh already carries mtime-normalize@1; @2 supersedes it"
        )
    if parent_setup_sh.count(ANCHOR) != 1:
        raise VariantInvalid("setup.sh has no unique ready sentinel; refusing to normalize mtimes")
    return parent_setup_sh.replace(ANCHOR, MTIME_BLOCK_V2.rstrip("\n") + "\n" + ANCHOR, 1)


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


def build_changes_v2(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @2.

    Only ``environment/setup/setup.sh`` and the ``task.toml`` healthcheck
    payload change; grading (``tests/``) is byte-identical by construction.
    """
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
        "fixed_mtime": FIXED_MTIME,
        "max_passes": MAX_PASSES_V2,
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_mtime_normalize_v2(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Set every work-tree path to one fixed timestamp at the end of setup "
        "so fix-commit file mtimes cannot name the fix. Materialize lazy "
        "layers first, retry touch+check up to 3 passes, and fail setup if "
        "any path's mtime differs from the fixed timestamp; grading is "
        "unchanged."
    ),
    created_by: str = "mtime-normalize",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``mtime-normalize@2`` variant of a MiMo task package.

    The parent may be the snapshot package or a strip/purge variant of it,
    without a ``@1``/``@2`` block; the block anchors on the ready sentinel,
    which all of them retain, so the runtime order is always strip, then
    purge, then normalize.
    """
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


__all__ = [
    "ANCHOR",
    "FIXED_MTIME",
    "MARKER",
    "MARKER_V2",
    "MAX_PASSES_V2",
    "MTIME_BLOCK",
    "MTIME_BLOCK_V2",
    "SETUP_REL",
    "TOUCH_STAMP",
    "TRANSFORM_ID",
    "TRANSFORM_ID_V2",
    "build_changes",
    "build_changes_v2",
    "build_setup_sh",
    "build_setup_sh_v2",
    "derive_mtime_normalize",
    "derive_mtime_normalize_v2",
]
