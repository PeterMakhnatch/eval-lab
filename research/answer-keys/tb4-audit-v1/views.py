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
from evallab.registry import _should_ignore_file, task_directory_digest

KEY_ID = "tb4-audit-v1"
VIEW_SCHEMA = "tb4.audit.agent-view/v1"
SPLIT_SEED = 20261020
_TASK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_TASK_INPUT_ROOTS = {"instruction.md", "task.toml", "tests", "solution", "environment"}
# Scorer-only witness programs and their registry never enter an auditor view.
_SCORER_ONLY_NAMES = {"witnesses.json", "witnesses"}
INPUT_SCHEMA = "tb4.audit.task_input/v1"
PUBLIC_TAXONOMY_SCHEMA = "tb4.audit.public-taxonomy/v1"
_INPUT_KEYS = {"schema", "key_id", "split", "subject", "package"}
_SUBJECT_KEYS = {"kind", "task_id", "task_digest"}
_PACKAGE_KEYS = {"name", "pin", "version"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TAXONOMY_KEYS = {"schema", "version", "label_rules", "decision_order", "task_side", "run_side"}
_TAXONOMY_CLASS_KEYS = {
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
}


def _scorer_only(relative: str) -> bool:
    return any(part in _SCORER_ONLY_NAMES for part in PurePosixPath(relative).parts)


def public_taxonomy(taxonomy: Mapping[str, Any]) -> dict[str, Any]:
    """Remove case examples, label counts, source links and per-finding labels."""
    return {
        "schema": PUBLIC_TAXONOMY_SCHEMA,
        "version": taxonomy["version"],
        "label_rules": taxonomy["label_rules"],
        "decision_order": taxonomy["decision_order"],
        **{
            side: [
                {key: row[key] for key in sorted(_TAXONOMY_CLASS_KEYS) if key in row}
                for row in taxonomy[side]
            ]
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
            "schema": INPUT_SCHEMA,
            "key_id": KEY_ID,
            "split": split,
            "subject": {
                "kind": "task",
                "task_id": task_id,
                "task_digest": subject.get("task_digest"),
            },
            "package": {"name": name, "pin": package.get("pin"), "version": package.get("version")},
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
        if _scorer_only(relative):
            raise ValueError("scorer-only witness files cannot enter an auditor view")
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
            # Files outside the package digest (caches, editor temp files) are not inputs.
            if _should_ignore_file(path):
                continue
            relative = relative_path.as_posix()
            if ".git" in PurePosixPath(relative).parts:
                raise ValueError("repository history cannot enter an auditor view")
            if _scorer_only(relative):
                continue
            retain(f"tasks/{name}/{relative}", path.read_bytes())
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


def _check_inventory_path(relative: str) -> str | None:
    """Return the task name of a task file, or None for a root input; reject anything else."""
    name = PurePosixPath(relative)
    if name.is_absolute() or ".." in name.parts or ":" in relative or "\\" in relative:
        raise ValueError("the auditor view inventory contains an unsafe path")
    if _scorer_only(relative):
        raise ValueError("scorer-only witness files cannot enter an auditor view")
    if relative in {"inputs.jsonl", "taxonomy.json"}:
        return None
    parts = name.parts
    if (
        len(parts) < 3
        or parts[0] != "tasks"
        or not _TASK_NAME.match(parts[1])
        or parts[2] not in _TASK_INPUT_ROOTS
        or (len(parts) == 3 and parts[2] not in {"instruction.md", "task.toml"})
        or ".git" in parts
        or _should_ignore_file(Path(*parts))
    ):
        raise ValueError(f"the auditor view inventory lists a non-input file: {relative}")
    return parts[1]


def _check_inputs(data: bytes, manifest: Mapping[str, Any], task_dirs: set[str]) -> None:
    records = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    names = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != _INPUT_KEYS:
            raise ValueError("an auditor input record has unexpected fields")
        subject, package = record["subject"], record["package"]
        if (
            record["schema"] != INPUT_SCHEMA
            or record["key_id"] != KEY_ID
            or record["split"] != manifest["split"]
            or not isinstance(subject, dict)
            or set(subject) != _SUBJECT_KEYS
            or not isinstance(package, dict)
            or set(package) != _PACKAGE_KEYS
            or not isinstance(package["name"], str)
            or not _TASK_NAME.match(package["name"])
            or subject["kind"] != "task"
            or subject["task_id"] != f"terminal-bench/{package['name']}"
            or not isinstance(subject["task_digest"], str)
            or not _DIGEST.match(subject["task_digest"])
        ):
            raise ValueError("an auditor input record has unexpected fields or values")
        names.add(package["name"])
    if len(records) != manifest.get("task_count") or len(names) != len(records):
        raise ValueError("the auditor input count disagrees with the manifest")
    if manifest.get("upstream_task_files_included") and task_dirs != names:
        raise ValueError("the exported task directories disagree with the auditor inputs")
    if not manifest.get("upstream_task_files_included") and task_dirs:
        raise ValueError("task files are present in a view exported without them")


def _check_taxonomy(data: bytes) -> None:
    taxonomy = json.loads(data)
    if (
        not isinstance(taxonomy, dict)
        or set(taxonomy) != _TAXONOMY_KEYS
        or taxonomy["schema"] != PUBLIC_TAXONOMY_SCHEMA
    ):
        raise ValueError("the public taxonomy has unexpected fields")
    for side in ("task_side", "run_side"):
        entries = taxonomy[side]
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict) or not set(entry) <= _TAXONOMY_CLASS_KEYS
            for entry in entries
        ):
            raise ValueError("the public taxonomy has unexpected class fields")


def _validate_view_root(root: Path) -> tuple[bytes, dict[str, str]]:
    """Check manifest semantics, the exact file set, input schemas and file digests."""
    _reject_symlinks(root)
    manifest_path = root / "bundle.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("the auditor view manifest must be a regular file")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != VIEW_SCHEMA
        or manifest.get("key_id") != KEY_ID
    ):
        raise ValueError("not a TB4 audit agent view")
    if manifest.get("split") not in {"dev", "test"}:
        raise ValueError("the auditor view split must be dev or test")
    inventory = manifest.get("files")
    if not isinstance(inventory, dict) or "inputs.jsonl" not in inventory:
        raise ValueError("the auditor view has no task input inventory")
    task_dirs = {task for relative in inventory if (task := _check_inventory_path(relative))}
    present = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("view paths must not traverse symlinks")
        if path.is_file():
            present.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise ValueError("auditor view entries must be regular files")
    if present - {"bundle.json"} != set(inventory):
        raise ValueError("the auditor view contains files not listed in its manifest")
    contents = {}
    for relative, expected in inventory.items():
        path = root / relative
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("an auditor view file is not retained under its root")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("the auditor view changed after export")
        if relative in {"inputs.jsonl", "taxonomy.json"}:
            contents[relative] = data
    _check_inputs(contents["inputs.jsonl"], manifest, task_dirs)
    if "taxonomy.json" in contents:
        _check_taxonomy(contents["taxonomy.json"])
    return raw, dict(inventory)


class SealedWorkspaceTools(WorkspaceTools):
    """Read-only tools that re-check the sealed view before every call."""

    def __init__(self, root: Path, manifest: bytes, inventory: dict[str, str]) -> None:
        super().__init__(root, set(inventory))
        self._manifest = manifest
        self._inventory = inventory
        self._stats = {
            rel: (st.st_ino, st.st_size, st.st_mtime_ns)
            for rel in inventory
            if (st := (root / rel).lstat())
        }

    def _recheck_large_view(self, target_prefix: str) -> None:
        _reject_symlinks(self.root)
        manifest_path = self.root / "bundle.json"
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.read_bytes() != self._manifest
        ):
            raise ValueError("the auditor view changed after export")
        present = set()
        for path in self.root.rglob("*"):
            if path.is_symlink():
                raise ValueError("view paths must not traverse symlinks")
            if path.is_file():
                present.add(path.relative_to(self.root).as_posix())
            elif not path.is_dir():
                raise ValueError("auditor view entries must be regular files")
        if present - {"bundle.json"} != set(self._inventory):
            raise ValueError("the auditor view contains files not listed in its manifest")
        for rel, expected in self._inventory.items():
            path = self.root / rel
            st = path.lstat()
            if (
                (st.st_ino, st.st_size, st.st_mtime_ns) != self._stats.get(rel)
                or rel in {"inputs.jsonl", "taxonomy.json"}
                or (target_prefix and (rel == target_prefix or rel.startswith(target_prefix + "/")))
            ) and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("the auditor view changed after export")

    def run(self, tool: str, args: dict[str, Any]) -> str:
        if len(self._inventory) <= 250:
            manifest, inventory = _validate_view_root(self.root)
            if manifest != self._manifest or inventory != self._inventory:
                raise ValueError("the auditor view changed after export")
        else:
            raw_target = str(args.get("path", "")).strip().removeprefix("/workspace/").strip("/")
            self._recheck_large_view(raw_target)
        return super().run(tool, args)

def agent_tools(directory: Path) -> SealedWorkspaceTools:
    """Load the read-only capability after checking the exported bytes and schemas."""
    root = Path(directory).absolute()
    manifest, inventory = _validate_view_root(root)
    return SealedWorkspaceTools(root, manifest, inventory)
