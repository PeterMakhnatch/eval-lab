"""Snapshot system DBs for history checks (``general-pinned-backup@1``).

Fifty tasks contain baseline helper references. No fetched workplace or setup
creates the file. sqlite3.connect instead creates an empty database; baseline
comparisons can then fail on pristine state. The census measures each item.

The block backs up each live system DB at the end of setup, before the agent.
SQLite's backup API includes committed WAL state; copying the main file alone
would not. It fails setup closed for an absent or unreadable database.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "general-pinned-backup@1"

#: Marker comment identifying the inserted block (also the idempotence guard).
MARKER = "general-pinned-backup@1"

#: Package-relative path of the setup script this transform rewrites.
SETUP_REL = "environment/setup/setup.sh"

#: Ready sentinel; the block runs just before it, at the end of setup --
#: after the workplace fetch and the MCP server start, so the baseline is
#: the exact agent-start state.
ANCHOR = 'touch "$M/ready"'

#: The MCP services are already running: capture committed WAL data consistently.
SNAPSHOT_BLOCK = """\
# general-pinned-backup@1: capture a consistent pre-agent system baseline.
python3 - <<'PBB_PY' || fail "general-pinned-backup@1 cannot snapshot system databases"
import sqlite3
from pathlib import Path
dbs = sorted(Path("/work/system").glob("*/state.db"))
if not dbs:
    raise RuntimeError("no system databases to snapshot")
for db in dbs:
    target = db.with_name("state.db.pinned_backup")
    target.unlink(missing_ok=True)
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(target) as snapshot:
            source.backup(snapshot)
PBB_PY
"""


def build_setup_sh(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the snapshot block. Returns input unchanged when present."""
    if MARKER in parent_setup_sh:
        return parent_setup_sh
    if parent_setup_sh.count(ANCHOR) != 1:
        raise VariantInvalid("setup.sh has no unique ready sentinel; refusing to snapshot DBs")
    return parent_setup_sh.replace(ANCHOR, SNAPSHOT_BLOCK.rstrip("\n") + "\n" + ANCHOR, 1)


def _read_parent(parent_dir: Path) -> tuple[str, str, str]:
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
    return task_name, workdir, setup_sh


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Only ``environment/setup/setup.sh`` and the ``task.toml`` healthcheck
    payload change; grading (``tests/``) is byte-identical by construction.
    """
    parent = Path(parent_dir)
    task_name, workdir, setup_sh = _read_parent(parent)
    new_setup = build_setup_sh(setup_sh)
    if new_setup == setup_sh:
        raise VariantInvalid("parent setup.sh already carries general-pinned-backup@1")
    new_blob = pack_setup(parent / "environment" / "setup", setup_sh=new_setup.encode("utf-8"))
    parent_toml = (parent / "task.toml").read_text(encoding="utf-8")
    changes: dict[str, bytes | None] = {
        SETUP_REL: new_setup.encode("utf-8"),
        "task.toml": render_task_toml(parent_toml, new_blob=new_blob).encode("utf-8"),
    }
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "workdir": workdir,
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_general_pinned_backup(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Snapshot every system state.db to state.db.pinned_backup at the end "
        "of setup so history checks read a real agent-start baseline instead "
        "of an empty auto-created database. Fail setup if any snapshot is "
        "missing; grading is unchanged."
    ),
    created_by: str = "general-pinned-backup",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``general-pinned-backup@1`` variant of a general task package."""
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
    "MARKER",
    "SETUP_REL",
    "SNAPSHOT_BLOCK",
    "TRANSFORM_ID",
    "build_changes",
    "build_setup_sh",
    "derive_general_pinned_backup",
]
