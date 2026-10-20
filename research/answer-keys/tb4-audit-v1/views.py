"""Task-disjoint assignments and label-free inputs for the TB4 audit key.

The full answer key is scorer-side. Auditors receive only an exported view and
``WorkspaceTools``; giving an auditor a host shell, this repository, review URLs,
or a network tool is outside this sealed evaluation protocol.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from evallab.qa.ask import WorkspaceTools
from evallab.registry import task_directory_digest

KEY_ID = "tb4-audit-v1"
VIEW_SCHEMA = "tb4.audit.agent-view/v1"
SPLIT_SEED = 20261020
_TASK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_TASK_INPUT_ROOTS = {"instruction.md", "task.toml", "tests", "solution", "environment"}


def public_taxonomy(taxonomy: Mapping[str, Any]) -> dict[str, Any]:
    """Remove case examples, label counts, source links and per-finding labels."""
    fields = (
        "id",
        "key",
        "name",
        "definition",
        "direction",
        "fix_location",
        "evidence_standard",
        "not_this",
        "detection_methods",
        "enabled_by",
    )
    return {
        "schema": "tb4.audit.public-taxonomy/v1",
        "version": taxonomy["version"],
        "label_rules": taxonomy["label_rules"],
        "decision_order": taxonomy["decision_order"],
        **{
            side: [{key: row[key] for key in fields if key in row} for row in taxonomy[side]]
            for side in ("task_side", "run_side")
        },
    }


def task_splits(
    tasks: Iterable[str], *, seed: int = SPLIT_SEED, dev_count: int = 25
) -> dict[str, str]:
    """Assign whole tasks, without consulting their findings or source labels."""
    names = list(tasks)
    if len(names) != len(set(names)):
        raise ValueError("the task cohort must contain unique task names")
    if not 0 < dev_count < len(names):
        raise ValueError("dev_count must leave at least one task in each split")
    ranked = sorted(
        names,
        key=lambda name: (
            hashlib.sha256(f"{KEY_ID}:{seed}:{name}".encode()).hexdigest(),
            name,
        ),
    )
    development = set(ranked[:dev_count])
    return {name: "dev" if name in development else "test" for name in sorted(names)}


def _reject_symlinks(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("view paths must not traverse symlinks")


def _public_tasks(items: Iterable[Mapping[str, Any]], split: str) -> list[dict[str, Any]]:
    if split not in {"dev", "test"}:
        raise ValueError("the view split must be dev or test")
    tasks: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("split") != split or item.get("scope", "task") != "task":
            continue
        subject = item["subject"]
        task_id = subject["task_id"]
        name = task_id.rsplit("/", 1)[-1]
        if not _TASK_NAME.fullmatch(name):
            raise ValueError("task names must be ordinary benchmark directory names")
        package = item.get("package") or {}
        if package.get("name", name) != name:
            raise ValueError("the package name and task identity disagree")
        # Deliberate whitelist: no finding IDs, titles, classes, source reviews,
        # outcomes, port tests, root causes, or per-task finding counts.
        public = {
            "input_id": hashlib.sha256(f"{KEY_ID}:{task_id}".encode()).hexdigest()[:24],
            "subject": {
                "kind": "task",
                "task_id": task_id,
                "task_digest": subject.get("task_digest"),
            },
            "package": {"name": name, "pin": package.get("pin")},
        }
        if task_id in tasks and tasks[task_id] != public:
            raise ValueError("findings for one task disagree about its package")
        tasks[task_id] = public
    if not tasks:
        raise ValueError("the requested split contains no task-scoped inputs")
    return [tasks[task_id] for task_id in sorted(tasks)]


def materialize_agent_view(
    items: Iterable[Mapping[str, Any]],
    destination: Path,
    *,
    split: str,
    task_root: Path | None = None,
    taxonomy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Export whitelisted task inputs, optionally with pinned upstream packages.

    A new destination is required; an existing view or another writer's files
    are never overwritten. The only copied upstream surfaces are instruction,
    task.toml, tests, reference solution and environment. Maintainer READMEs,
    cheat directories, port tests and audit source caches are excluded.
    """
    public = _public_tasks(items, split)
    destination = Path(destination).absolute()
    _reject_symlinks(destination)
    if destination.exists():
        raise FileExistsError("a new destination is required for a sealed view")
    source_root = Path(task_root).resolve(strict=True) if task_root is not None else None
    destination.mkdir(parents=True)
    inventory: dict[str, str] = {}

    def retain(relative: str, data: bytes) -> None:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        target.chmod(0o444)
        inventory[relative] = hashlib.sha256(data).hexdigest()

    for task in public:
        if source_root is None:
            continue
        name = task["package"]["name"]
        source = source_root / name
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"the upstream task directory is missing or symlinked: {name}")
        if not source.resolve(strict=True).is_relative_to(source_root):
            raise ValueError("an upstream task leaves the declared task root")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError("a pinned upstream task must not contain symlinks")
        expected_digest = task["subject"]["task_digest"]
        if not expected_digest or task_directory_digest(source) != expected_digest:
            raise ValueError(
                f"the upstream task does not match its recorded package digest: {name}"
            )
        for path in sorted(source.rglob("*")):
            relative_path = path.relative_to(source)
            if relative_path.parts[0] not in _TASK_INPUT_ROOTS:
                continue
            if path.is_symlink():
                raise ValueError(f"upstream task symlinks cannot enter an auditor view: {name}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("upstream task inputs must be regular files")
            relative = relative_path.as_posix()
            if ".git" in PurePosixPath(relative).parts:
                raise ValueError("repository history cannot enter an auditor view")
            retain(f"tasks/{name}/{relative}", path.read_bytes())
        task["files_root"] = f"tasks/{name}"
    retain(
        "inputs.jsonl",
        "".join(json.dumps(task, sort_keys=True) + "\n" for task in public).encode(),
    )
    if taxonomy is not None:
        retain(
            "taxonomy.json",
            (json.dumps(public_taxonomy(taxonomy), indent=2, sort_keys=True) + "\n").encode(),
        )
    manifest = {
        "schema": VIEW_SCHEMA,
        "key_id": KEY_ID,
        "split": split,
        "task_count": len(public),
        "upstream_task_files_included": source_root is not None,
        "task_file_surfaces": sorted(_TASK_INPUT_ROOTS),
        "files": dict(sorted(inventory.items())),
        "capability": "evallab.qa.ask.WorkspaceTools: read, grep, diff only",
    }
    with (destination / "bundle.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (destination / "bundle.json").chmod(0o444)
    return manifest


def agent_tools(directory: Path) -> WorkspaceTools:
    """Load the existing read-only capability after checking the exported bytes."""
    root = Path(directory).absolute()
    _reject_symlinks(root)
    manifest_path = root / "bundle.json"
    if manifest_path.is_symlink():
        raise ValueError("the auditor view manifest must be a regular file")
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("schema") != VIEW_SCHEMA or manifest.get("key_id") != KEY_ID:
        raise ValueError("not a TB4 audit agent view")
    inventory = manifest["files"]
    if not isinstance(inventory, dict) or "inputs.jsonl" not in inventory:
        raise ValueError("the auditor view has no task input inventory")
    for relative, expected in inventory.items():
        name = PurePosixPath(relative)
        if name.is_absolute() or ".." in name.parts or ":" in relative or "\\" in relative:
            raise ValueError("the auditor view inventory contains an unsafe path")
        path = root / relative
        _reject_symlinks(path)
        if not path.is_file() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("an auditor view file is not retained under its root")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("the auditor view changed after export")
    return WorkspaceTools(root, set(inventory))
