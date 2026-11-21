from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "mtime-normalize@1"

MARKER = "mtime-normalize@1"

TRANSFORM_ID_V2 = "mtime-normalize@2"

MARKER_V2 = "mtime-normalize@2"

MAX_PASSES_V2 = 3

SETUP_REL = "environment/setup/setup.sh"

ANCHOR = 'touch "$M/ready"'

FIXED_MTIME = "2000-01-01 00:00:00 UTC"


MTIME_BLOCK = """\
# mtime-normalize@1: one fixed timestamp for every work-tree path, so fix-commit mtimes cannot leak.
find "$CWD" -exec touch -h -t 200001010000.00 {} + || fail "mtime-normalize@1 cannot normalize timestamps"
[ -z "$(find "$CWD" -newermt '2000-01-01' -print -quit)" ] || fail "mtime-normalize@1 found paths newer than the fixed timestamp"
"""

def build_setup_sh(parent_setup_sh: str) -> str:
    if MARKER in parent_setup_sh:
        return parent_setup_sh
    if parent_setup_sh.count(ANCHOR) != 1:
        raise VariantInvalid("setup.sh has no unique ready sentinel; refusing to normalize mtimes")
    return parent_setup_sh.replace(ANCHOR, MTIME_BLOCK.rstrip("\n") + "\n" + ANCHOR, 1)


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
