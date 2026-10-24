from __future__ import annotations

import ast
import hashlib
import io
import stat
import tarfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

from evallab import candidate_port as port
from evallab.upstream_grader import GraderRefusal, _tree_digest


URL = "https://example.invalid/pinned-public-data"
KEEP = "lambda m: not Path(m.name).name.startswith('._')"


def put(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


@pytest.fixture
def packages(tmp_path: Path) -> tuple[Path, Path]:
    original = tmp_path / "original" / "native-task"
    fixed = tmp_path / "fixed" / "native-task"
    put(original / "task.toml", '''
        schema_version = "1.0"
        artifacts = ["/app/result.txt"]
        [task]
        name = "terminal-bench/native-task"
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
    put(original / "instruction.md", "Write result.txt.\n")
    put(original / "environment/Dockerfile", "FROM scratch\n")
    put(fixed / "Containerfile", "FROM scratch\nCOPY shared_data/ /shared_data/\nCOPY root_data/ /root_data/\n")
    put(fixed / "pyproject.toml", '[project]\nname="environment"\nversion="0.1.0"\n')
    put(fixed / "uv.lock", "version = 1\n")
    put(fixed / ".manifest.json", "{}\n")
    put(fixed / "shared_data/.gitkeep", "")
    put(fixed / "root_data/.gitkeep", "")
    put(fixed / "src/environment/tasks/native_task/__init__.py", '''
        class NativeTask(Task):
            id = "native-task"
            def required_hardware(self):
                return "cpus=2,memory_mb=4096,storage_mb=10240"
            def judge(self):
                return ExecutableJudge(["python", "scoring_script.py"])
    ''')
    return original, fixed


def cache_blob(root: Path, data: bytes) -> tuple[Path, str]:
    digest = hashlib.sha256(data).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    (root / digest).write_bytes(data)
    return root, digest


def fetch_recipe(fixed: Path, digest: str, suffix: str = "") -> None:
    put(fixed / "setup_data.py", f'''
        def setup(ctx):
            ctx.fetch({URL!r}, ctx.root_data / "blob.bin", {digest!r})
            {suffix}
    ''')


def tar_member(
    name: str,
    data: bytes = b"",
    *,
    kind: bytes = tarfile.REGTYPE,
    mode: int = 0o644,
    link: str = "",
) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.type = kind
    member.mode = mode
    member.linkname = link
    member.size = len(data) if member.isfile() else 0
    return member, data


def tar_bytes(*members: tuple[tarfile.TarInfo, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as bundle:
        for member, data in members:
            bundle.addfile(member, io.BytesIO(data) if member.isfile() else None)
    return buffer.getvalue()


def tar_recipe(fixed: Path, digest: str, keep: str | None = KEEP, suffix: str = "") -> None:
    keyword = f", keep={keep}" if keep is not None else ""
    put(fixed / "setup_data.py", f'''
        def setup(ctx):
            ctx.fetch_tar({URL!r}, {digest!r}, ctx.shared_data / "google_fonts"{keyword})
            {suffix}
    ''')


def test_external_blob_materializes_exact_bytes_with_path_independent_identity(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    data = b"\x00pinned\xffexternal\nbytes\x00"
    first_cache, digest = cache_blob(tmp_path / "cache-one", data)
    second_cache, _ = cache_blob(tmp_path / "unrelated/cache-two", data)
    (first_cache / digest).chmod(0o755)
    fetch_recipe(fixed, digest, '(ctx.root_data / "copy.bin").write_bytes((ctx.root_data / "blob.bin").read_bytes())')
    before = [_tree_digest(path) for path in packages]
    described = port.describe_port_task(original, fixed, asset_cache=first_cache)
    first = port.prepare_port_task(original, fixed, tmp_path / "one/task", asset_cache=first_cache)
    second = port.prepare_port_task(original, fixed, tmp_path / "two/task", asset_cache=str(second_cache))
    for result in (first, second):
        tests = Path(result["task_dir"]) / "tests"
        assert (tests / "root_data/blob.bin").read_bytes() == data
        assert (tests / "root_data/copy.bin").read_bytes() == data
        assert stat.S_IMODE((tests / "root_data/blob.bin").stat().st_mode) == 0o644
        assert str(tmp_path) not in (tests / "candidate-port-config.json").read_text()
    assert first["grader_digest"] == second["grader_digest"] == described["grader_digest"]
    assert first["native_image_identity"]["verifier"]["logical_digest"] == second["native_image_identity"]["verifier"]["logical_digest"]
    assert first["native_image_identity"]["verifier"]["exact_context_digest"] == second["native_image_identity"]["verifier"]["exact_context_digest"]
    asset = described["setup_data"]["assets"][0]
    assert (asset["source_url"], asset["sha256"], asset["source_kind"]) == (URL, digest, "asset_cache")
    assert asset["source"] == str(first_cache / digest)
    assert [_tree_digest(path) for path in packages] == before
    assert (first_cache / digest).read_bytes() == data


def test_missing_explicit_cache_never_discovers_or_downloads(packages: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    original, fixed = packages
    cache, digest = cache_blob(tmp_path / "assets", b"already present but not opted in")
    fetch_recipe(fixed, digest)
    monkeypatch.chdir(cache)
    monkeypatch.setenv("ASSET_CACHE", str(cache))

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("offline port attempted a network download")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal, match=f"sha256:{digest}"):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()
    with pytest.raises(GraderRefusal, match=f"sha256:{digest}"):
        port.prepare_port_task(original, fixed, output, asset_cache=tmp_path / "missing")
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["tampered", "directory", "symlink", "cache-symlink"])
def test_cache_integrity_refuses_before_output(packages: tuple[Path, Path], tmp_path: Path, mutation: str) -> None:
    original, fixed = packages
    cache, digest = cache_blob(tmp_path / "assets", b"trusted bytes")
    fetch_recipe(fixed, digest)
    source = cache / digest
    if mutation == "tampered":
        source.write_bytes(b"wrong bytes under the right filename")
    elif mutation == "directory":
        source.unlink()
        source.mkdir()
    elif mutation == "symlink":
        target = tmp_path / "outside"
        target.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(target)
    else:
        alias = tmp_path / "cache-alias"
        alias.symlink_to(cache, target_is_directory=True)
        cache = alias
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal, match="sha256|symlink"):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()


def test_cache_is_rechecked_after_describe_and_after_lookup(packages: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original, fixed = packages
    data = b"trusted bytes"
    cache, digest = cache_blob(tmp_path / "assets", data)
    fetch_recipe(fixed, digest)
    port.describe_port_task(original, fixed, asset_cache=cache)
    (cache / digest).write_bytes(b"tampered after description")
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal, match="differs from pinned"):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()
    (cache / digest).write_bytes(data)
    lookup = port._SetupRecipe._cached_asset

    def changed_after_lookup(self: port._SetupRecipe, pin: str) -> Path:
        path = lookup(self, pin)
        path.write_bytes(b"changed immediately after lookup")
        return path

    monkeypatch.setattr(port._SetupRecipe, "_cached_asset", changed_after_lookup)
    with pytest.raises(GraderRefusal, match="differs from pinned"):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()


@pytest.mark.parametrize("digest", ["../secret", "f" * 63, "F" * 64, "sha256:../../secret", "sha256:" + "a" * 64])
def test_cache_key_must_be_an_exact_hash(packages: tuple[Path, Path], tmp_path: Path, digest: str) -> None:
    original, fixed = packages
    fetch_recipe(fixed, digest)
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal, match="exact.*sha256"):
        port.prepare_port_task(original, fixed, output, asset_cache=tmp_path / "assets")
    assert not output.exists()


def test_tar_preserves_layout_empty_dirs_data_modes_and_extraction_marker(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    archive = tar_bytes(
        tar_member("./", kind=tarfile.DIRTYPE, mode=0o700),
        tar_member("fonts", kind=tarfile.DIRTYPE, mode=0o700),
        tar_member("fonts/NotoSans.ttf", b"font bytes"),
        tar_member("fonts/tool", b"#!/bin/sh\n", mode=0o777),
        tar_member("fonts/non-owner-executable", b"data", mode=0o641),
        tar_member("empty", kind=tarfile.DIRTYPE, mode=0),
        tar_member("._top-metadata", b"discard"),
        tar_member("fonts/._metadata", b"discard"),
        tar_member("._directory", kind=tarfile.DIRTYPE),
        tar_member("._directory/real-font.ttf", b"basename filter keeps this"),
    )
    cache, digest = cache_blob(tmp_path / "cache-one", archive)
    second_cache, _ = cache_blob(tmp_path / "cache-two", archive)
    tar_recipe(fixed, digest)
    put(fixed / "shared_data/google_fonts/stale.txt", "must be removed by fetch_tar\n")
    before = [_tree_digest(path) for path in packages]
    first = port.prepare_port_task(original, fixed, tmp_path / "first/task", asset_cache=cache)
    second = port.prepare_port_task(original, fixed, tmp_path / "second/task", asset_cache=second_cache)
    root = Path(first["task_dir"]) / "tests/shared_data/google_fonts"
    assert (root / "fonts/NotoSans.ttf").read_bytes() == b"font bytes"
    assert (root / "._directory/real-font.ttf").read_bytes() == b"basename filter keeps this"
    assert (root / "empty").is_dir()
    assert stat.S_IMODE((root / "empty").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "fonts").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "fonts/tool").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "fonts/non-owner-executable").stat().st_mode) == 0o640
    assert not (root / "stale.txt").exists()
    assert not (root / "._top-metadata").exists()
    assert not (root / "fonts/._metadata").exists()
    marker = root / f".extracted-{digest[:16]}"
    assert marker.read_bytes() == b""
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644
    assert first["grader_digest"] == second["grader_digest"]
    assert first["native_image_identity"]["verifier"]["exact_context_digest"] == second["native_image_identity"]["verifier"]["exact_context_digest"]
    external = first["setup_data"]["external_inputs"][0]
    assert (external["source_url"], external["sha256"], external["filter"]) == (URL, digest, "exclude-appledouble-basename")
    assert [_tree_digest(path) for path in packages] == before


def test_tar_default_filter_and_verified_marker_short_circuit(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    cache, digest = cache_blob(tmp_path / "assets", tar_bytes(tar_member("._retained", b"keep by default")))
    tar_recipe(fixed, digest, keep=None, suffix=f'ctx.fetch_tar({URL!r}, {digest!r}, ctx.shared_data / "google_fonts")')
    prepared = port.prepare_port_task(original, fixed, tmp_path / "task", asset_cache=cache)
    root = Path(prepared["task_dir"]) / "tests/shared_data/google_fonts"
    assert (root / "._retained").read_bytes() == b"keep by default"
    assert len(prepared["setup_data"]["external_inputs"]) == 1


@pytest.mark.parametrize("members,reason", [
    ([tar_member("../escape", b"bad")], "unsafe.*path"),
    ([tar_member("/absolute", b"bad")], "unsafe.*path"),
    ([tar_member("fonts/../../escape", b"bad")], "unsafe.*path"),
    ([tar_member("fonts\\escape", b"bad")], "unsafe.*path"),
    ([tar_member("link", kind=tarfile.SYMTYPE, link="outside")], "link/special"),
    ([tar_member("hardlink", kind=tarfile.LNKTYPE, link="outside")], "link/special"),
    ([tar_member("pipe", kind=tarfile.FIFOTYPE)], "link/special"),
    ([tar_member("device", kind=tarfile.CHRTYPE)], "link/special"),
    ([tar_member("fonts/a", b"one"), tar_member("./fonts/a", b"two")], "overwrite/conflict"),
    ([tar_member("fonts", b"file"), tar_member("fonts/a", b"nested")], "overwrite/conflict"),
    ([tar_member("fonts/a", b"nested"), tar_member("fonts", b"file")], "overwrite/conflict"),
    ([tar_member("fonts", kind=tarfile.DIRTYPE), tar_member("fonts", kind=tarfile.DIRTYPE)], "overwrite/conflict"),
    ([tar_member("setuid", b"bad", mode=0o4755)], "unsafe permission"),
    ([tar_member("directory-shaped/", b"bad")], "directory-shaped"),
    ([tar_member("._excluded-link", kind=tarfile.SYMTYPE, link="outside")], "link/special"),
])
def test_unsafe_tar_members_refuse_before_output(packages: tuple[Path, Path], tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes]], reason: str) -> None:
    original, fixed = packages
    cache, digest = cache_blob(tmp_path / "assets", tar_bytes(*members))
    tar_recipe(fixed, digest)
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal, match=reason):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("keep", ["lambda m: m.isfile()", "lambda m: not m.name.startswith('._')", "unknown_predicate", "lambda m, extra=True: True"])
def test_unknown_tar_filters_refuse_before_cache_or_output(packages: tuple[Path, Path], tmp_path: Path, keep: str) -> None:
    original, fixed = packages
    tar_recipe(fixed, "a" * 64, keep=keep)
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal, match="deterministic keep filter"):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()


def test_pinned_non_archive_and_port_file_collision_refuse_before_output(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    cache, digest = cache_blob(tmp_path / "assets", b"not a tar archive")
    tar_recipe(fixed, digest)
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal, match="readable pinned tar"):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()
    cache, digest = cache_blob(cache, tar_bytes(tar_member("font.ttf", b"font")))
    tar_recipe(fixed, digest)
    put(fixed / "shared_data/google_fonts", "a file cannot become the extraction directory")
    with pytest.raises(GraderRefusal, match="conflicts with a port file"):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()


def test_archive_word_in_helper_does_not_authorize_skipping_its_assertion(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    cache, digest = cache_blob(tmp_path / "assets", tar_bytes(tar_member("font.ttf", b"font")))
    put(fixed / "setup_data.py", f'''
        def not_an_extractor(archive, destination):
            unrelated = "tarfile"
            assert False, "mandatory safety assertion"
        def setup(ctx):
            ctx.fetch({URL!r}, ctx.root_data / "archive.tar.xz", {digest!r})
            not_an_extractor(ctx.root_data / "archive.tar.xz", ctx.shared_data / "fonts")
    ''')
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()


def test_cached_asset_helper_accepts_canonical_image_digest_without_ambient_state(tmp_path: Path) -> None:
    cache, digest = cache_blob(tmp_path / "assets", b"opaque layer bytes")
    recipe = port._SetupRecipe(ast.parse(textwrap.dedent(f'''
        def setup(ctx):
            ctx.fetch({URL!r}, ctx.root_data / "blob", {digest!r})
    ''')), "native-task", asset_cache=cache)
    assert recipe._cached_asset("sha256:" + digest) == cache / digest
    (cache / digest).write_bytes(b"tampered")
    with pytest.raises(GraderRefusal, match="differs from pinned"):
        recipe._cached_asset("sha256:" + digest)


def test_output_cannot_overlap_asset_custody(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    cache, digest = cache_blob(tmp_path / "assets", b"pinned")
    fetch_recipe(fixed, digest)
    output = cache / "prepared-task"
    with pytest.raises(GraderRefusal, match="outside.*asset cache"):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()
    assert (cache / digest).read_bytes() == b"pinned"


def test_unverified_existing_extraction_marker_is_not_an_asset_cache(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    cache, digest = cache_blob(tmp_path / "assets", tar_bytes(tar_member("font.ttf", b"font")))
    tar_recipe(fixed, digest)
    put(fixed / f"shared_data/google_fonts/.extracted-{digest[:16]}", "")
    output = tmp_path / "must-not-exist"
    with pytest.raises(GraderRefusal, match="without verified extraction custody"):
        port.prepare_port_task(original, fixed, output, asset_cache=cache)
    assert not output.exists()
