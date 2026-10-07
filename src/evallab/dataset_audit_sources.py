"""Resolve real Harbor packages without executing or inventing task evidence."""

from __future__ import annotations

import hashlib
import os
import tempfile
import tomllib
from pathlib import Path

from evallab.dataset_audit_contracts import AuditDataset, AuditSource, AuditTask
from evallab.dataset_audit_plugins import normalize_task_id
from evallab.fetch import (
    MANIFEST_NAME,
    STATE_NAME,
    FetchState,
    detect_git_origin,
    detect_license,
    load_state,
    material_digest,
    parse_pin,
    render_manifest,
    write_state,
)
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_import import _package_files, discover_task_packages


def file_source(path: Path, *, binding: str | None = None) -> AuditSource:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return AuditSource(path=str(path), available=False, binding=binding)
    return AuditSource(
        path=str(path), sha256="sha256:" + hashlib.sha256(raw).hexdigest(), binding=binding
    )


def _source_uri(path: Path, root: Path) -> str:
    try:
        return "repo:" + path.relative_to(root).as_posix()
    except ValueError:
        return path.as_uri()


def _metadata_root(path: Path, repo_root: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / STATE_NAME).is_file():
            return candidate
        if candidate == repo_root:
            break
    return None


def _download(ref: str, destination: Path, repo_root: Path) -> None:
    """Explicit acquisition only; the native executor owns the subprocess."""
    from evallab.queue import Executor

    pin = parse_pin(ref)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".audit-fetch-", dir=destination.parent) as scratch:
        staged = Path(scratch) / "dataset"
        Executor.from_repo(repo_root, create_queue=False).download_dataset(
            pin.ref, staged, output_to_stderr=True
        )
        tasks = discover_task_packages(staged)
        if not tasks:
            raise ValueError(f"{pin.ref}: download contained no Harbor task packages")
        git_url, git_sha = detect_git_origin(staged)
        state = FetchState(
            pin=pin.ref,
            version=pin.version,
            task_git_url=git_url,
            task_git_sha=git_sha,
            tree_digest=material_digest(staged),
            task_count=len(tasks),
            lane="hub",
            license=detect_license(staged),
        )
        write_state(staged, state)
        (staged / MANIFEST_NAME).write_text(
            render_manifest(state, dest_rel=_source_uri(destination, repo_root), inner_note=""),
            encoding="utf-8",
        )
        if destination.exists():
            raise FileExistsError(f"dataset destination appeared during acquisition: {destination}")
        os.rename(staged, destination)


def resolve_harbor_dataset(
    selector: str, *, repo_root: Path, download: bool = False
) -> AuditDataset:
    """Inspect a local path or cached pin; only explicit execution may acquire it."""
    root = repo_root.resolve()
    supplied = Path(selector).expanduser()
    candidate = supplied if supplied.is_absolute() else root / supplied
    explicit_path = candidate.is_dir()
    requested_pin: str | None = None
    if explicit_path:
        selected = candidate.resolve()
    else:
        pin = parse_pin(selector) if "@" in selector else None
        name = pin.name if pin is not None else selector
        benches = (root / "library/benchmarks").resolve()
        selected = (benches / name).resolve()
        if not selected.is_relative_to(benches) or selected == benches:
            raise ValueError(f"invalid Harbor dataset selector: {selector!r}")
        requested_pin = pin.ref if pin is not None else None
        if not selected.is_dir():
            if not download:
                raise ValueError(
                    f"dataset is not local: {selector!r}; dry-run never downloads. "
                    "Use an existing task directory or --execute with an explicit name@version."
                )
            if requested_pin is None:
                raise ValueError("dataset acquisition requires an explicit name@version")
            _download(requested_pin, selected, root)

    metadata_root = _metadata_root(selected, root)
    state = load_state(metadata_root) if metadata_root is not None else None
    sources: list[AuditSource] = []
    bound_pin = False
    if state is not None and metadata_root is not None:
        if requested_pin is not None and state.pin != requested_pin:
            raise ValueError(f"cached dataset is {state.pin}, not requested {requested_pin}")
        bound_pin = material_digest(metadata_root) == state.tree_digest
        if not explicit_path and not bound_pin:
            raise ValueError(f"cached dataset bytes no longer match recorded {state.pin}")
        sources.append(file_source(
            metadata_root / STATE_NAME,
            binding="material-digest-matched" if bound_pin else "modified-local-material",
        ))
    elif requested_pin is not None:
        raise ValueError(f"cached dataset has no provenance for requested {requested_pin}")

    source_uri = f"harbor:{state.pin}" if state is not None and bound_pin else _source_uri(selected, root)
    dataset_id = (
        state.pin if state is not None and bound_pin
        else selected.name + "-" + hashlib.sha256(source_uri.encode()).hexdigest()[:12]
    )
    revision = (state.task_git_sha or state.version) if state is not None and bound_pin else None
    source_root = metadata_root if bound_pin and metadata_root is not None else selected
    packages = discover_task_packages(selected)
    if not packages:
        raise ValueError(f"no Harbor task.toml packages found under {selected}")
    tasks: list[AuditTask] = []
    seen: set[str] = set()
    for package in packages:
        # Use the existing importer boundary before digesting potentially linked
        # files; source packages are neither copied nor rewritten by inspection.
        _package_files(package)
        config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
        declared = config.get("task")
        name = declared.get("name") if isinstance(declared, dict) else None
        if declared is not None and not isinstance(name, str):
            raise ValueError(f"invalid native task name in {package / 'task.toml'}")
        native_name = name if name is not None else package.name
        task_id = normalize_task_id(native_name)
        if task_id in seen:
            raise ValueError(f"ambiguous duplicate task identity in dataset: {task_id}")
        seen.add(task_id)
        try:
            parent_path = package.relative_to(root).as_posix()
        except ValueError:
            parent_path = str(package)
        relative = package.relative_to(source_root).as_posix()
        tasks.append(AuditTask(
            dataset_id=dataset_id,
            task_id=task_id,
            task_name=native_name,
            path=package,
            package_digest=task_directory_digest(package),
            harbor_digest=harbor_task_digest(package),
            aliases=(native_name,),
            source_uri=source_uri + "/" + relative,
            revision=revision,
            parent_source={"kind": "local", "path": parent_path},
        ))
    return AuditDataset(
        dataset_id=dataset_id,
        source_uri=source_uri,
        revision=revision,
        license=state.license if state is not None else None,
        tasks=tuple(tasks),
        sources=tuple(sources),
    )
