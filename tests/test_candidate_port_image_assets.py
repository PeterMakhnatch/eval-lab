from __future__ import annotations

import gzip
import hashlib
import io
import json
import stat
import tarfile
import textwrap
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from evallab import candidate_port as port
from evallab import upstream_grader as grader


def _put(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


@pytest.fixture
def image_packages(tmp_path: Path) -> tuple[Path, Path]:
    original = tmp_path / "original/image-task"
    fixed = tmp_path / "fixed/image-task"
    _put(original / "task.toml", '''
        schema_version = "1.0"
        artifacts = ["/app/result.bin"]
        [task]
        name = "terminal-bench/image-task"
        [agent]
        timeout_sec = 60.0
        [verifier]
        timeout_sec = 60.0
        environment_mode = "shared"
        [environment]
        cpus = 2
        memory_mb = 4096
        storage_mb = 10240
        gpus = 0
        build_timeout_sec = 600.0
    ''')
    _put(original / "instruction.md", "Produce the requested output.\n")
    _put(original / "environment/Dockerfile", "FROM scratch\nWORKDIR /app\n")
    _put(fixed / "Containerfile", "FROM scratch\nCOPY src/ /root/src/\nCOPY intermediate_data/ /root/intermediate_data/\n")
    _put(fixed / "pyproject.toml", '[project]\nname="environment"\nversion="0.1.0"\nrequires-python=">=3.12"\ndependencies=["karotte==3.0.75"]\n')
    _put(fixed / "uv.lock", 'version = 1\nrevision = 1\nrequires-python = ">=3.12"\n')
    _put(fixed / ".manifest.json", '{"karotte_version":"3.0.75","agents":[],"templates":["default"]}\n')
    _put(fixed / "intermediate_data/.gitkeep", "")
    _put(fixed / "src/environment/tasks/image_task/__init__.py", '''
        from karotte import Task
        from karotte.judges import ExecutableJudge

        class ImageTask(Task):
            id = "image-task"
            @property
            def required_hardware(self):
                return "cpus=2,memory_mb=4096,storage_mb=10240"
            @property
            def judge(self):
                return ExecutableJudge(["/root/grader"])
    ''')
    return original, fixed


def _member(name: str, content: bytes = b"", *, mode: int = 0o755, kind: bytes = tarfile.REGTYPE, link: str = "") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.linkname = link
    info.size = len(content) if kind == tarfile.REGTYPE else 0
    return info, content


def _cache_layer(cache: Path, members: list[tuple[tarfile.TarInfo, bytes]], *, compressed: bool = False) -> tuple[str, Path]:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        for info, content in members:
            bundle.addfile(info, io.BytesIO(content) if info.isfile() else None)
    data = buffer.getvalue()
    if compressed:
        data = gzip.compress(data, mtime=0)
    digest = hashlib.sha256(data).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    blob = cache / digest
    blob.write_bytes(data)
    return "sha256:" + digest, blob


def _install_recipe(fixed: Path, layer: str, files: dict[str, tuple[str, str]], *, registry: str | None = None) -> None:
    registry_argument = f", registry={registry!r}" if registry is not None else ""
    _put(fixed / "setup_data.py", f'''
        IMAGE_REPO = "trusted-fixture/data"
        IMAGE_LAYER = {layer!r}
        IMAGE_FILES = {files!r}
        def setup(ctx):
            files = {{member: (ctx.intermediate_data / destination, sha) for member, (destination, sha) in IMAGE_FILES.items()}}
            ctx.fetch_image_files(IMAGE_REPO, IMAGE_LAYER, files{registry_argument})
    ''')


def _refuses_before_output(packages: tuple[Path, Path], cache: Path, output: Path, pattern: str) -> None:
    before = [grader._tree_digest(path) for path in (*packages, cache)]
    with pytest.raises(grader.GraderRefusal, match=pattern):
        port.prepare_port_task(*packages, output, asset_cache=cache)
    assert not output.exists()
    assert [grader._tree_digest(path) for path in (*packages, cache)] == before


@pytest.mark.parametrize("compressed", [False, True])
def test_selected_layer_bytes_prepare_without_archive_modes_or_source_mutation(image_packages: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool) -> None:
    original, fixed = image_packages
    cache = tmp_path / "cache"
    contents = {"app/data/chunk.bin": b"\x00\x01trusted shard\xff", "app/.git/hooks/sample": b"#!/bin/sh\nexit 0\n"}
    layer, blob = _cache_layer(cache, [_member(name, data, mode=0o777 if index else 0o755) for index, (name, data) in enumerate(contents.items())], compressed=compressed)
    files = {name: (name, hashlib.sha256(data).hexdigest()) for name, data in contents.items()}
    _install_recipe(fixed, layer, files)
    before = [grader._tree_digest(path) for path in (*image_packages, cache)]

    def forbidden(*_: Any, **__: Any) -> None:
        pytest.fail("offline image materialization crossed a network or native execution boundary")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(grader, "_execute_native", forbidden)
    monkeypatch.setattr(grader, "_admit_docker", forbidden)
    monkeypatch.setattr(grader, "_reserve_spend", forbidden)
    described = port.describe_port_task(original, fixed, asset_cache=cache)
    prepared = port.prepare_port_task(original, fixed, tmp_path / "prepared", asset_cache=cache)
    tests = Path(prepared["task_dir"]) / "tests"
    reference = tmp_path / "source-helper-mode.part"
    reference.write_bytes(b"fresh source-helper file")
    reference_mode = stat.S_IMODE(reference.stat().st_mode)
    for name, content in contents.items():
        selected = tests / "intermediate_data" / name
        assert selected.read_bytes() == content
        assert stat.S_IMODE(selected.stat().st_mode) == reference_mode
    expected = [
        {
            "destination": "intermediate_data/" + name,
            "sha256": files[name][1],
            "provenance": {
                "kind": "oci-layer-member", "registry": "registry-1.docker.io", "repository": "trusted-fixture/data",
                "layer": layer, "member": name, "sha256": files[name][1],
            },
        }
        for name in sorted(contents)
    ]
    assert described["setup_data"]["generated"] == prepared["setup_data"]["generated"] == expected
    assert described["setup_data"]["assets"] == []
    assert prepared["grader_digest"] == described["grader_digest"]
    assert prepared["native_image_identity"]["verifier"]["logical_digest"] == described["native_image_identity"]["verifier"]["logical_digest"]
    assert "COPY intermediate_data/ /root/intermediate_data/" in (tests / "Dockerfile").read_text()
    assert str(cache) not in json.dumps(expected)
    assert str(cache) not in (tests / "candidate-port-config.json").read_text()
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == layer[7:]
    assert [grader._tree_digest(path) for path in (*image_packages, cache)] == before


def test_layer_identity_is_independent_of_cache_and_preparation_paths(image_packages: tuple[Path, Path], tmp_path: Path) -> None:
    _, fixed = image_packages
    cache = tmp_path / "first-cache"
    data = b"pinned bytes"
    name = "app/data/shard.bin"
    layer, blob = _cache_layer(cache, [_member(name, data)])
    _install_recipe(fixed, layer, {name: (name, hashlib.sha256(data).hexdigest())}, registry="offline.registry.example")
    other_cache = tmp_path / "other-cache"
    other_cache.mkdir()
    (other_cache / blob.name).write_bytes(blob.read_bytes())
    first = port.prepare_port_task(*image_packages, tmp_path / "first-output", asset_cache=cache)
    second = port.prepare_port_task(*image_packages, tmp_path / "second-output", asset_cache=str(other_cache))
    assert first["setup_data"]["generated"] == second["setup_data"]["generated"]
    assert first["setup_data"]["generated"][0]["provenance"]["registry"] == "offline.registry.example"
    assert first["grader_digest"] == second["grader_digest"]
    assert first["native_image_identity"]["verifier"]["logical_digest"] == second["native_image_identity"]["verifier"]["logical_digest"]
    assert first["native_image_identity"]["verifier"]["exact_context_digest"] == second["native_image_identity"]["verifier"]["exact_context_digest"]


def test_corrupt_cached_layer_refuses_before_output(image_packages: tuple[Path, Path], tmp_path: Path) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    name, data = "app/data/shard.bin", b"trusted shard"
    layer, blob = _cache_layer(cache, [_member(name, data)])
    _install_recipe(fixed, layer, {name: (name, hashlib.sha256(data).hexdigest())})
    blob.write_bytes(b"not the pinned layer")
    _refuses_before_output(image_packages, cache, tmp_path / "prepared", "sha256")


def test_blob_changed_after_cache_lookup_is_reverified(image_packages: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    name, data = "app/data/shard.bin", b"trusted shard"
    layer, _ = _cache_layer(cache, [_member(name, data)])
    _install_recipe(fixed, layer, {name: (name, hashlib.sha256(data).hexdigest())})
    lookup = port._SetupRecipe._cached_asset

    def replaced(recipe: Any, digest: str) -> Path:
        blob = lookup(recipe, digest)
        blob.write_bytes(b"changed after lookup")
        return blob

    monkeypatch.setattr(port._SetupRecipe, "_cached_asset", replaced)
    before = [grader._tree_digest(path) for path in image_packages]
    output = tmp_path / "prepared"
    with pytest.raises(grader.GraderRefusal, match="image-layer blob differs from pinned sha256"):
        port.prepare_port_task(*image_packages, output, asset_cache=cache)
    assert not output.exists()
    assert [grader._tree_digest(path) for path in image_packages] == before


def test_selected_member_hash_must_match_independently_of_blob_hash(image_packages: tuple[Path, Path], tmp_path: Path) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    name = "app/data/shard.bin"
    layer, _ = _cache_layer(cache, [_member(name, b"corrupt member")])
    _install_recipe(fixed, layer, {name: (name, hashlib.sha256(b"expected member").hexdigest())})
    _refuses_before_output(image_packages, cache, tmp_path / "prepared", "member differs from pinned sha256")


def test_missing_selected_member_refuses_before_output(image_packages: tuple[Path, Path], tmp_path: Path) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    data = b"trusted shard"
    layer, _ = _cache_layer(cache, [_member("app/data/first.bin", data)])
    digest = hashlib.sha256(data).hexdigest()
    _install_recipe(fixed, layer, {"app/data/first.bin": ("first.bin", digest), "app/data/missing.bin": ("missing.bin", digest)})
    _refuses_before_output(image_packages, cache, tmp_path / "prepared", "lacks selected member app/data/missing.bin")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_selected_nonregular_member_never_dereferences_or_extracts(image_packages: tuple[Path, Path], tmp_path: Path, kind: bytes) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    name, data = "app/data/shard.bin", b"trusted shard"
    layer, _ = _cache_layer(cache, [_member("app/data/target.bin", data), _member(name, kind=kind, link="app/data/target.bin")])
    _install_recipe(fixed, layer, {name: (name, hashlib.sha256(data).hexdigest())})
    _refuses_before_output(image_packages, cache, tmp_path / "prepared", "selected member is not a regular file")


@pytest.mark.parametrize("second_name", ["app/data/shard.bin", "./app/data/shard.bin", "app//data/shard.bin", "app/data/./shard.bin"])
def test_duplicate_or_aliased_members_refuse_before_output(image_packages: tuple[Path, Path], tmp_path: Path, second_name: str) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    name, data = "app/data/shard.bin", b"trusted shard"
    layer, _ = _cache_layer(cache, [_member(name, data), _member(second_name, data)])
    _install_recipe(fixed, layer, {name: (name, hashlib.sha256(data).hexdigest())})
    _refuses_before_output(image_packages, cache, tmp_path / "prepared", "duplicate or ambiguous member")


@pytest.mark.parametrize("unsafe_name", ["../escape.bin", "/absolute.bin", "app/../../escape.bin"])
def test_traversal_even_in_unselected_member_refuses_before_output(image_packages: tuple[Path, Path], tmp_path: Path, unsafe_name: str) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    name, data = "app/data/shard.bin", b"trusted shard"
    layer, _ = _cache_layer(cache, [_member(name, data), _member(unsafe_name, b"outside")])
    _install_recipe(fixed, layer, {name: (name, hashlib.sha256(data).hexdigest())})
    _refuses_before_output(image_packages, cache, tmp_path / "prepared", "unsafe setup-data path")
    assert not (tmp_path / "escape.bin").exists()


def test_member_selection_uses_exact_source_name_not_normalized_alias(image_packages: tuple[Path, Path], tmp_path: Path) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    name, data = "app/data/shard.bin", b"trusted shard"
    layer, _ = _cache_layer(cache, [_member("./" + name, data)])
    _install_recipe(fixed, layer, {name: (name, hashlib.sha256(data).hexdigest())})
    _refuses_before_output(image_packages, cache, tmp_path / "prepared", "lacks selected member app/data/shard.bin")


def test_hash_pinned_nonarchive_blob_refuses_before_output(image_packages: tuple[Path, Path], tmp_path: Path) -> None:
    _, fixed = image_packages
    cache = tmp_path / "cache"
    cache.mkdir()
    data = b"valid content identity, but not a tar layer"
    digest = hashlib.sha256(data).hexdigest()
    (cache / digest).write_bytes(data)
    _install_recipe(fixed, "sha256:" + digest, {"app/data/shard.bin": ("shard.bin", hashlib.sha256(b"expected").hexdigest())})
    _refuses_before_output(image_packages, cache, tmp_path / "prepared", "not a readable pinned tar archive")
