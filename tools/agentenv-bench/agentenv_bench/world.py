"""Fetch pinned world bytes and expose the upstream Python tools unchanged."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import re
import shutil
import tarfile
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

Call = Callable[[str, str, dict], Any]


def fetch_manifest(harbor_task_dir: Path) -> dict:
    """Decode fetch.json without executing the task's healthcheck shell command."""
    config = tomllib.loads((harbor_task_dir / "task.toml").read_text())
    command = config["environment"]["healthcheck"]["command"]
    match = re.search(r"\becho\s+([A-Za-z0-9+/=]+)\s*\|", command)
    if match is None:
        raise ValueError("healthcheck has no embedded base64 archive")
    archive = base64.b64decode(match.group(1), validate=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.getmember("files/fetch.json")
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError("embedded fetch.json is not a regular file")
        manifest = json.load(stream)
    if not manifest.get("dataset") or not manifest.get("revision") or not manifest.get("files"):
        raise ValueError("incomplete pinned fetch manifest")
    return manifest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, entry: dict) -> None:
    size = path.stat().st_size
    if size != entry["size"]:
        raise ValueError(f"size mismatch for {entry['target']}: {size} != {entry['size']}")
    if not entry.get("git_sha1") and not entry.get("sha256"):
        raise ValueError(f"no content hash for {entry['target']}")
    if entry.get("git_sha1"):
        digest = hashlib.sha1(f"blob {size}\0".encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry["git_sha1"]:
            raise ValueError(f"git blob SHA-1 mismatch for {entry['target']}")
    if entry.get("sha256") and sha256_file(path) != entry["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {entry['target']}")


def fetch_world(harbor_task_dir: Path, cache_dir: Path) -> Path:
    """Download every manifest file, rejecting corrupt downloads and cache entries."""
    manifest = fetch_manifest(harbor_task_dir)
    root = cache_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    targets: set[str] = set()
    for entry in manifest["files"]:
        relative = PurePosixPath(entry["target"].lstrip("/"))
        if not relative.parts or ".." in relative.parts or str(relative) in targets:
            raise ValueError(f"unsafe or duplicate world target: {entry['target']}")
        targets.add(str(relative))
        destination = root.joinpath(*relative.parts)
        if not destination.resolve().is_relative_to(root):
            raise ValueError(f"world target escapes cache: {entry['target']}")
        if destination.exists():
            _verify(destination, entry)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = (
            f"https://huggingface.co/datasets/{quote(manifest['dataset'], safe='/')}/resolve/"
            f"{quote(manifest['revision'], safe='')}/{quote(entry['path'], safe='/')}"
        )
        request = Request(url, headers={"User-Agent": "agentenv-bench/0.1 (model-free local controls)"})
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                with urlopen(request, timeout=120) as response:
                    shutil.copyfileobj(response, output)
            _verify(temporary, entry)
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if not (root / "work/tools").is_dir() or not (root / "work/system").is_dir():
        raise ValueError("manifest did not produce the expected work tree")
    return root


def materialize(world_root: Path, scratch: Path) -> Path:
    """Make a fresh isolated copy; never overwrite an existing control world."""
    work_dir = scratch / "work"
    if work_dir.exists():
        raise FileExistsError(f"control world already exists: {work_dir}")
    scratch.mkdir(parents=True, exist_ok=True)
    shutil.copytree(world_root / "work", work_dir)
    return work_dir


def direct_call(work_dir: Path) -> Call:
    """Call public upstream functions, preserving returned errors and exceptions."""
    modules: dict[str, Any] = {}
    work_dir = work_dir.resolve()

    def call(system: str, tool: str, args: dict) -> Any:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", system):
            raise ValueError(f"invalid system name: {system}")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", tool):
            raise ValueError(f"invalid public tool name: {tool}")
        if system not in modules:
            source = work_dir / "tools" / f"{system}.py"
            identity = hashlib.sha256(str(source).encode()).hexdigest()[:16]
            spec = importlib.util.spec_from_file_location(f"bench_{system}_{identity}", source)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load tool module: {source}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules[system] = module
        module = modules[system]
        function = getattr(module, tool)
        if not callable(function) or getattr(function, "__module__", None) != module.__name__:
            raise ValueError(f"not an upstream public function: {system}.{tool}")
        return function(**args)

    return call


def world_receipt(harbor_task_dir: Path, world_root: Path) -> dict:
    """Record expected and actual content hashes for every pinned input."""
    manifest = fetch_manifest(harbor_task_dir)
    files = []
    for entry in manifest["files"]:
        path = world_root / entry["target"].lstrip("/")
        _verify(path, entry)
        files.append(dict(entry, actual_sha256=sha256_file(path)))
    task_inputs = {}
    for relative in ("task.toml", "instruction.md", "tests/verifier/verifier_meta.json"):
        task_inputs[relative] = sha256_file(harbor_task_dir / relative)
    return {
        "harbor_task_dir": str(harbor_task_dir.resolve()),
        "dataset": manifest["dataset"],
        "revision": manifest["revision"],
        "task_input_sha256": task_inputs,
        "files": files,
    }
