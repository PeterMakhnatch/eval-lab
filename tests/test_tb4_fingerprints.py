"""Deterministic static-provenance boundaries; no network or package execution."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "research/experiments/tb4-provenance-20261020/fingerprint.py"


def load_fingerprint():
    spec = importlib.util.spec_from_file_location("tb4_fingerprint", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fingerprint():
    return load_fingerprint()


@pytest.fixture
def native_task_config():
    try:
        importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("native Harbor SDK regressions require uv sync --extra laminar")
    from harbor.models.task.config import TaskConfig

    return TaskConfig


def test_importable_harbor_stub_is_not_an_installed_native_sdk(monkeypatch, request):
    stub = ModuleType("harbor.models.task.config")
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    version = importlib.metadata.version

    def missing_harbor(name):
        if name == "harbor":
            raise importlib.metadata.PackageNotFoundError(name)
        return version(name)

    monkeypatch.setattr(importlib.metadata, "version", missing_harbor)
    assert pytest.importorskip(stub.__name__) is stub
    with pytest.raises(pytest.skip.Exception, match="native Harbor SDK"):
        request.getfixturevalue("native_task_config")


def task_tree(root: Path) -> Path:
    files = {
        "instruction.md": b"Produce the requested artifact.\n",
        "task.toml": b'version = "1.0"\n',
        "tests/test.py": b"assert artifact == expected\n",
        "environment/Dockerfile": b"FROM pinned-image\n",
        "solution/solve.sh": b"write-artifact\n",
        "README.md": b"Original public task support.\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def specification(fingerprint, source="terminal-bench/terminal-bench", task="terminal-bench/sample"):
    digest = "sha256:" + "a" * 64
    return {
        "key": fingerprint.package_key(source, digest, task),
        "source": source, "digest": digest, "task_name": task,
        "canonical_digest": digest, "dataset_refs": [{"name": source, "ref": "frozen-job-ref"}],
    }


def metadata_for(spec):
    source = spec["source"]
    return {
        source: {
            "content_hash": "sha256:" + "b" * 64,
            "tasks": [{
                "task_version_id": "native-task-version-id",
                "task_version": {
                    "content_hash": spec["digest"].removeprefix("sha256:"),
                    "package": {"name": "sample", "org": {"name": "terminal-bench"}},
                },
                "available": True,
            }],
        },
    }


def test_package_key_binds_the_exact_tuple_without_delimiter_ambiguity(fingerprint):
    expected = fingerprint.sha256(b'["org/source","sha256:abc","org/task"]')
    assert fingerprint.package_key("org/source", "sha256:abc", "org/task") == expected
    assert fingerprint.package_key("a,b", "c", "d") != fingerprint.package_key("a", "b,c", "d")
    assert fingerprint.package_key("a", "b", "c") != fingerprint.package_key("other", "b", "c")


@pytest.mark.parametrize("link_position", ["root", "ancestor"])
def test_cli_refuses_symlinked_output_before_resolving_or_native_calls(fingerprint, tmp_path, monkeypatch, link_position):
    outside = tmp_path / "outside-output"
    outside.mkdir()
    linked = tmp_path / "output-link"
    linked.symlink_to(outside, target_is_directory=True)
    output = linked if link_position == "root" else linked / "packages"
    native_calls = []

    def unexpected_native(*args):
        native_calls.append(args)
        pytest.fail("A symlinked output must be refused before creating a native client")

    monkeypatch.setattr(fingerprint, "NativeHarbor", unexpected_native)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--output", str(output)])
    with pytest.raises(fingerprint.IntegrityError, match="Symlink target is forbidden"):
        fingerprint.main()
    assert native_calls == []
    assert list(outside.iterdir()) == []


def test_full_inventory_includes_dotfiles_modes_and_empty_directories(fingerprint, tmp_path):
    root = task_tree(tmp_path / "task")
    (root / "tests/.hidden-support").write_bytes(b"retained bytes\n")
    (root / "environment/empty").mkdir()
    (root / "tests/test.py").chmod(0o755)
    entries = fingerprint.inventory(root)
    assert entries["tests/.hidden-support"]["sha256"] == fingerprint.sha256(b"retained bytes\n")
    assert entries["tests/test.py"]["mode"] == 0o755
    assert entries["environment/empty"]["kind"] == "directory"


def test_identical_trees_are_byte_equal_without_claiming_runtime_equivalence(fingerprint, tmp_path):
    original = task_tree(tmp_path / "original")
    upstream = task_tree(tmp_path / "upstream")
    result = fingerprint.compare_inventories(fingerprint.inventory(original), fingerprint.inventory(upstream))
    assert result["byte_and_path_equal"] is True
    assert result["metadata_equal"] is True
    assert all(result["categories"][category]["equal"] is True for category in fingerprint.CATEGORIES)
    assert "runtime_equivalence" not in result


def test_equal_common_verifier_does_not_hide_missing_nested_test_support(fingerprint, tmp_path):
    original = task_tree(tmp_path / "original")
    upstream = task_tree(tmp_path / "upstream")
    support = upstream / "tests/data/.source-checksum"
    support.parent.mkdir()
    support.write_bytes(b"original support\n")
    result = fingerprint.compare_inventories(fingerprint.inventory(original), fingerprint.inventory(upstream))
    assert result["categories"]["instruction"]["equal"] is True
    assert result["categories"]["tests"]["equal"] is False
    assert result["missing_from_package"] == ["tests/data", "tests/data/.source-checksum"]
    assert result["byte_and_path_equal"] is False


def test_remaining_support_and_same_size_content_changes_are_not_filtered(fingerprint, tmp_path):
    original = task_tree(tmp_path / "original")
    upstream = task_tree(tmp_path / "upstream")
    (original / "task.toml").write_bytes(b'version = "2.0"\n')
    (upstream / "LICENSE").write_bytes(b"public license\n")
    result = fingerprint.compare_inventories(fingerprint.inventory(original), fingerprint.inventory(upstream))
    assert result["categories"]["tests"]["equal"] is True
    assert result["categories"]["config"]["equal"] is False
    assert result["categories"]["support"]["equal"] is False
    assert result["changed_paths"] == ["task.toml"]
    assert result["missing_from_package"] == ["LICENSE"]


def test_mode_differences_are_separate_from_content_identity(fingerprint, tmp_path):
    original = task_tree(tmp_path / "original")
    upstream = task_tree(tmp_path / "upstream")
    (original / "tests/test.py").chmod(0o644)
    (upstream / "tests/test.py").chmod(0o755)
    result = fingerprint.compare_inventories(fingerprint.inventory(original), fingerprint.inventory(upstream))
    assert result["categories"]["tests"]["equal"] is True
    assert result["byte_and_path_equal"] is True
    assert result["metadata_equal"] is False
    assert result["metadata_changed_paths"] == ["tests/test.py"]
    assert result["categories"]["tests"]["metadata_changed_paths"] == ["tests/test.py"]


def test_link_metadata_is_separate_when_contained_target_bytes_match(fingerprint, tmp_path):
    original = task_tree(tmp_path / "original")
    upstream = task_tree(tmp_path / "upstream")
    content = (original / "tests/test.py").read_bytes()
    (original / "tests/target.py").write_bytes(content)
    (upstream / "tests/target.py").write_bytes(content)
    (original / "tests/test.py").unlink()
    (original / "tests/test.py").symlink_to("target.py")
    left, right = fingerprint.inventory(original), fingerprint.inventory(upstream)
    result = fingerprint.compare_inventories(left, right)
    assert left["tests/test.py"]["symlink"] == "target.py"
    assert left["tests/test.py"]["sha256"] == right["tests/test.py"]["sha256"]
    assert result["categories"]["tests"]["equal"] is True
    assert result["metadata_equal"] is False
    assert "tests/test.py" in result["metadata_changed_paths"]


def test_external_symlink_bytes_are_never_read_or_vacuously_equal(fingerprint, tmp_path, monkeypatch):
    root = tmp_path / "task"
    (root / "tests").mkdir(parents=True)
    external = tmp_path / "external"
    external.write_bytes(b"not part of the package")
    (root / "tests/link").symlink_to(external)
    original_hash = fingerprint.file_hash
    calls = []

    def contained_hash(path):
        calls.append(path)
        assert path != external
        return original_hash(path)

    monkeypatch.setattr(fingerprint, "file_hash", contained_hash)
    entries = fingerprint.inventory(root)
    assert calls == []
    assert entries["tests/link"]["sha256"] is None
    assert entries["tests/link"]["symlink"] == str(external)
    result = fingerprint.compare_inventories(entries, entries)
    assert result["categories"]["tests"]["equal"] is None
    assert result["unmeasured_paths"] == ["tests/link"]
    assert result["byte_and_path_equal"] is None


@pytest.mark.parametrize("linked_side", ["package", "upstream"])
def test_byte_diff_never_reads_below_external_directory_symlinks(fingerprint, tmp_path, monkeypatch, linked_side):
    package, upstream = tmp_path / "package", tmp_path / "upstream"
    for root in (package, upstream):
        (root / "tests").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "test.py").write_text("outside package boundaries\n")
    linked, ordinary = (package, upstream) if linked_side == "package" else (upstream, package)
    (linked / "tests/nested").symlink_to(external, target_is_directory=True)
    (ordinary / "tests/nested").mkdir()
    (ordinary / "tests/nested/test.py").write_text("ordinary measured file\n")
    comparison = fingerprint.compare_inventories(fingerprint.inventory(package), fingerprint.inventory(upstream))
    read_bytes = Path.read_bytes
    escaped_reads = []

    def contained_read(path):
        if path.resolve().is_relative_to(external):
            escaped_reads.append(path)
            raise AssertionError("diff attempted to read outside its package root")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", contained_read)
    diff = fingerprint.text_diff(package, upstream, comparison)
    assert escaped_reads == []
    assert b"Type or directory change: tests/nested/test.py\n" in diff
    assert b"outside package boundaries" not in diff
    assert comparison["categories"]["tests"]["equal"] is False


def test_missing_both_required_categories_is_unmeasured(fingerprint):
    result = fingerprint.compare_inventories({}, {})
    for category in ("instruction", "tests", "environment", "config", "reference"):
        assert result["categories"][category]["equal"] is None
    assert result["categories"]["support"]["equal"] is True


@pytest.mark.parametrize(
    ("expected", "resolved", "computed", "status"),
    [
        ("sha256:" + "a" * 64, "a" * 64, "a" * 64, "bound"),
        ("sha256:" + "a" * 64, "a" * 64, None, "unmeasured"),
        ("sha256:" + "a" * 64, "a" * 64, "b" * 64, "unmeasured"),
        ("sha256:" + "a" * 64, "b" * 64, "a" * 64, "unmeasured"),
        (None, "a" * 64, "a" * 64, "unmeasured"),
        ("latest", "a" * 64, "a" * 64, "unmeasured"),
        ("sha256:bad", "bad", "bad", "unmeasured"),
    ],
)
def test_only_resolved_and_measured_exact_lock_digest_binds(fingerprint, expected, resolved, computed, status):
    assert fingerprint.binding_status(expected, resolved, computed) == status


def test_unbound_observed_comparison_cannot_publish_verified_equalities(fingerprint):
    comparison = fingerprint.compare_inventories({}, {})
    comparison["categories"]["tests"]["equal"] = True
    assert all(value is None for value in fingerprint.qualified_equalities("unmeasured", comparison).values())
    assert fingerprint.qualified_equalities("bound", comparison)["tests_equal"] is True


def test_immutable_originals_are_never_repaired_or_overwritten(fingerprint, tmp_path):
    path = tmp_path / "original"
    fingerprint.immutable_write(path, b"original bytes")
    fingerprint.immutable_write(path, b"original bytes")
    with pytest.raises(fingerprint.IntegrityError, match="Existing evidence differs"):
        fingerprint.immutable_write(path, b"changed bytes")
    assert path.read_bytes() == b"original bytes"
    linked = tmp_path / "linked"
    linked.symlink_to(path)
    with pytest.raises(fingerprint.IntegrityError, match="Symlink evidence destination"):
        fingerprint.immutable_write(linked, b"original bytes")


def test_qualify_retains_full_diff_and_binds_exact_native_pin(fingerprint, tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    original = task_tree(tmp_path / "original")
    task_tree(tmp_path / "upstream/sample")
    (original / "task.toml").write_bytes(b'version = "2.0"\n')
    spec = specification(fingerprint)
    calls = []

    def download(root, pin, native):
        calls.append(pin)
        return original, {
            "computed_native_digest": "a" * 64, "inventory": fingerprint.inventory(original),
            "pin": pin, "receipt_path": "fixture-receipt.json",
        }

    monkeypatch.setattr(fingerprint, "download_package", download)
    result = fingerprint.qualify(
        output, tmp_path / "upstream", [spec], metadata_for(spec),
        {spec["source"]: {"path": "fixture-native-version.stdout"}}, {}, object(),
    )
    entry = result["packages"][0]
    assert calls == ["terminal-bench/sample@" + spec["digest"]]
    assert entry["status"] == "measured" and entry["binding_status"] == "bound"
    assert entry["tests_equal"] is True and entry["config_equal"] is False
    assert entry["comparison"]["metadata_equal"] is True
    delta = (output / entry["diff"]["path"]).read_bytes()
    assert b'-version = "1.0"' in delta and b'+version = "2.0"' in delta
    assert fingerprint.sha256(delta) == entry["diff"]["sha256"]
    assert json.loads((output / "fingerprints.json").read_bytes())["schema"] == fingerprint.SCHEMA
    assert result["boundaries"]["runtime_equivalence"] == "not_measured_or_claimed"


def test_current_alias_with_different_digest_is_explicitly_unmeasured(fingerprint, tmp_path, monkeypatch):
    spec = specification(fingerprint)
    metadata = metadata_for(spec)
    metadata[spec["source"]]["tasks"][0]["task_version"]["content_hash"] = "c" * 64

    def forbidden_download(*args):
        pytest.fail("An alias with the wrong historical digest must not download a substitute")

    monkeypatch.setattr(fingerprint, "download_package", forbidden_download)
    result = fingerprint.qualify(tmp_path, tmp_path / "upstream", [spec], metadata, {}, {}, object())
    entry = result["packages"][0]
    assert entry["status"] == "unmeasured" and entry["binding_status"] == "unmeasured"
    assert entry["tests_equal"] is None
    assert "historical task digest" in entry["unmeasured_reason"]


def test_cross_source_digest_recovery_never_proves_declared_membership(
    fingerprint, tmp_path, monkeypatch,
):
    spec = specification(fingerprint)
    alternate = "ryanmarten/terminal-bench-4-no-prebuild"
    metadata = metadata_for(spec)
    metadata[spec["source"]]["tasks"][0]["task_version"]["content_hash"] = "c" * 64
    edge = metadata_for(spec)[spec["source"]]["tasks"][0]
    edge["task_version"]["package"] = {
        "name": "terminal-bench-4-no-prebuild-sample", "org": {"name": "ryanmarten"},
    }
    metadata[alternate] = {"content_hash": "sha256:" + "d" * 64, "tasks": [edge]}
    original = task_tree(tmp_path / "original")
    task_tree(tmp_path / "upstream/sample")
    calls = []

    def download(root, pin, native):
        calls.append(pin)
        return original, {
            "computed_native_digest": "a" * 64, "inventory": fingerprint.inventory(original),
            "pin": pin, "receipt_path": "fixture-receipt.json",
        }

    monkeypatch.setattr(fingerprint, "download_package", download)
    result = fingerprint.qualify(
        tmp_path / "output", tmp_path / "upstream", [spec], metadata,
        {spec["source"]: {}, alternate: {}}, {}, object(),
    )
    entry = result["packages"][0]
    assert entry["source"] == "terminal-bench/terminal-bench"
    assert entry["status"] == "measured" and entry["binding_status"] == "bound"
    assert entry["binding_scope"] == "native_packager_digest_bytes_not_source_membership"
    assert entry["actual_export_source"] == alternate
    assert entry["actual_export_ref"] == calls[0]
    assert calls == ["ryanmarten/terminal-bench-4-no-prebuild-sample@" + spec["digest"]]
    assert entry["declared_source_binding"] == "unproven"
    assert entry["recorded_source_matches_actual_export_source"] is False
    assert entry["canonical_dataset_eligible"] is False
    assert entry["instruction_equal"] is True and entry["tests_equal"] is True
    assert entry["historical_full_inventory_binding"] == "unmeasured"
    assert result["coverage"]["source_membership_unproven_among_digest_bound"] == 1


def test_wrong_tuple_key_is_rejected_before_acquisition(fingerprint, tmp_path, monkeypatch):
    spec = specification(fingerprint)
    spec["key"] = "not-the-source-digest-task-key"
    monkeypatch.setattr(fingerprint, "download_package", lambda *args: pytest.fail("Wrong key"))
    with pytest.raises(fingerprint.IntegrityError, match="exact source/digest/task_name"):
        fingerprint.qualify(tmp_path, tmp_path / "upstream", [spec], {}, {}, {}, object())


def test_successful_export_is_receipted_and_changed_original_is_not_adopted(fingerprint, tmp_path, monkeypatch):
    packager_module = ModuleType("harbor.publisher.packager")

    class FakePackager:
        @staticmethod
        def compute_content_hash(root):
            return "a" * 64, [path for path in sorted(root.rglob("*")) if path.is_file()]

    packager_module.Packager = FakePackager
    monkeypatch.setitem(sys.modules, "harbor.publisher.packager", packager_module)

    class NativeFixture:
        calls = 0

        def _run(self, args):
            self.calls += 1
            assert args[:2] == ["task", "download"]
            assert args[2] == "terminal-bench/sample@sha256:" + "a" * 64
            export = Path(args[args.index("--output-dir") + 1])
            task_tree(export / "sample")
            return b"Downloaded original package.\n"

    native = NativeFixture()
    pin = "terminal-bench/sample@sha256:" + "a" * 64
    root, receipt = fingerprint.download_package(tmp_path, pin, native)
    assert root is not None and receipt["status"] == "complete"
    assert receipt["native_hash_paths"] and native.calls == 1
    again, same = fingerprint.download_package(tmp_path, pin, native)
    assert again == root and same == receipt and native.calls == 1
    (root / "tests/test.py").write_bytes(b"changed original")
    with pytest.raises(fingerprint.IntegrityError, match="Native original package changed"):
        fingerprint.download_package(tmp_path, pin, native)
    assert native.calls == 1
    assert (root / "tests/test.py").read_bytes() == b"changed original"


@pytest.mark.parametrize("artifact_kind", ["index", "receipt", "stdout", "parent"])
def test_download_cache_rejects_symlinked_evidence_before_reading(
    fingerprint, tmp_path, monkeypatch, artifact_kind,
):
    packager_module = ModuleType("harbor.publisher.packager")
    packager_module.Packager = SimpleNamespace(
        compute_content_hash=lambda root: ("a" * 64, sorted(path for path in root.rglob("*") if path.is_file())),
    )
    monkeypatch.setitem(sys.modules, "harbor.publisher.packager", packager_module)

    class NativeFixture:
        calls = 0

        def _run(self, args):
            self.calls += 1
            export = Path(args[args.index("--output-dir") + 1])
            task_tree(export / "sample")
            return b"Original native stdout.\n"

    cache = tmp_path / "cache"
    native = NativeFixture()
    pin = "terminal-bench/sample@sha256:" + "a" * 64
    original, receipt = fingerprint.download_package(cache, pin, native)
    assert original is not None and receipt["status"] == "complete"
    receipt_path = cache / receipt["receipt_path"]
    target = {
        "index": cache / "download-index" / f"{fingerprint.sha256(pin.encode())}.json",
        "receipt": receipt_path,
        "stdout": receipt_path.parent / "download.stdout",
        "parent": receipt_path.parent,
    }[artifact_kind]
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = outside / target.name
    target.rename(destination)
    target.symlink_to(destination, target_is_directory=destination.is_dir())
    original_open = Path.open
    escaped_reads = []

    def contained_open(path, *args, **kwargs):
        if path.resolve().is_relative_to(outside):
            escaped_reads.append(path)
            raise AssertionError("cache attempted to read an outside-root symlink target")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", contained_open)
    with pytest.raises(fingerprint.IntegrityError, match="Symlink target is forbidden"):
        fingerprint.download_package(cache, pin, native)
    assert escaped_reads == [] and native.calls == 1
    assert target.is_symlink()


def test_unreceipted_attempt_is_preserved_not_adopted(fingerprint, tmp_path, monkeypatch):
    packager_module = ModuleType("harbor.publisher.packager")

    class FakePackager:
        @staticmethod
        def compute_content_hash(root):
            return "a" * 64, [root / "instruction.md"]

    packager_module.Packager = FakePackager
    monkeypatch.setitem(sys.modules, "harbor.publisher.packager", packager_module)
    pin = "terminal-bench/sample@sha256:" + "a" * 64
    abandoned = tmp_path / "downloads" / fingerprint.sha256(pin.encode())
    abandoned.mkdir(parents=True)
    original = abandoned / "unreceipted-original"
    original.write_bytes(b"preserve partial original")

    class NativeFixture:
        def _run(self, args):
            export = Path(args[args.index("--output-dir") + 1])
            task_tree(export / "sample")
            return b"Fresh native acquisition.\n"

    root, receipt = fingerprint.download_package(tmp_path, pin, NativeFixture())
    assert root is not None and not root.is_relative_to(abandoned)
    assert receipt["status"] == "complete"
    assert original.read_bytes() == b"preserve partial original"
    assert not (abandoned / "receipt.json").exists()


def test_native_refusal_stops_further_reads_without_faking_coverage(fingerprint, tmp_path, monkeypatch):
    first = specification(fingerprint)
    second = specification(fingerprint, task="terminal-bench/second")
    calls = []

    def refused(root, pin, native):
        calls.append(pin)
        return None, {"status": "failed", "error": "Native Harbor command failed (exit 1)"}

    monkeypatch.setattr(fingerprint, "download_package", refused)
    result = fingerprint.qualify(
        tmp_path, tmp_path / "upstream", [first, second], metadata_for(first),
        {first["source"]: {}}, {}, object(),
    )
    assert len(calls) == 1
    assert result["coverage"]["distinct_packages_measured"] == 0
    assert result["coverage"]["distinct_packages_unmeasured"] == 2
    assert result["native_refusal"] == "Native Harbor command failed (exit 1)"
    assert all(entry["tests_equal"] is None for entry in result["packages"])
    assert all(entry["status"] == "unmeasured" for entry in result["packages"])


def test_absent_upstream_is_unmeasured_even_after_digest_binding(fingerprint, tmp_path, monkeypatch):
    spec = specification(fingerprint)
    original = task_tree(tmp_path / "original")
    monkeypatch.setattr(fingerprint, "download_package", lambda *args: (
        original, {
            "computed_native_digest": "a" * 64, "inventory": fingerprint.inventory(original),
            "pin": "terminal-bench/sample@" + spec["digest"], "receipt_path": "fixture-receipt.json",
        },
    ))
    result = fingerprint.qualify(
        tmp_path, tmp_path / "missing-upstream", [spec], metadata_for(spec),
        {spec["source"]: {}}, {}, object(),
    )
    entry = result["packages"][0]
    assert entry["binding_status"] == "bound" and entry["status"] == "unmeasured"
    assert entry["tests_equal"] is None and entry["instruction_equal"] is None
    assert entry["unmeasured_reason"] == "Exact upstream task directory unavailable"


def test_native_metadata_pin_and_public_visibility_are_enforced(fingerprint, tmp_path):
    class NativeFixture:
        def json(self, args):
            assert args == [
                "version", "show",
                "terminal-bench/terminal-bench@" + fingerprint.DATASET_DIGEST, "--tasks",
            ]
            return fingerprint.json_bytes({
                "package": "terminal-bench/terminal-bench", "type": "dataset",
                "visibility": "public", "content_hash": "sha256:" + "c" * 64,
                "tasks": [],
            })

    with pytest.raises(fingerprint.IntegrityError, match="did not resolve exactly"):
        fingerprint.dataset_receipt(tmp_path, "terminal-bench/terminal-bench", NativeFixture())
    assert (tmp_path / "bootstrap/canonical-dataset.stdout").is_file()


def test_receipted_native_metadata_resumes_offline_and_rejects_changed_bytes(
    fingerprint, tmp_path,
):
    class NativeFixture:
        calls = 0

        def json(self, args):
            self.calls += 1
            return fingerprint.json_bytes({
                "package": "terminal-bench/terminal-bench", "type": "dataset",
                "visibility": "public", "content_hash": fingerprint.DATASET_DIGEST,
                "tasks": [],
            })

    native = NativeFixture()
    first, receipt = fingerprint.dataset_receipt(
        tmp_path, "terminal-bench/terminal-bench", native,
    )
    second, resumed = fingerprint.dataset_receipt(
        tmp_path, "terminal-bench/terminal-bench", native,
    )
    assert first == second and receipt == resumed and native.calls == 1
    stdout = tmp_path / receipt["path"]
    stdout.write_bytes(b'{"changed": true}\n')
    with pytest.raises(fingerprint.IntegrityError, match="Dataset receipt changed"):
        fingerprint.dataset_receipt(tmp_path, "terminal-bench/terminal-bench", native)
    assert native.calls == 1


@pytest.mark.parametrize("artifact_kind", ["stdout", "receipt", "parent"])
def test_dataset_cache_rejects_symlinked_evidence_before_reading(
    fingerprint, tmp_path, monkeypatch, artifact_kind,
):
    class NativeFixture:
        calls = 0

        def json(self, args):
            self.calls += 1
            return fingerprint.json_bytes({
                "package": "terminal-bench/terminal-bench", "type": "dataset",
                "visibility": "public", "content_hash": fingerprint.DATASET_DIGEST,
                "tasks": [],
            })

    cache = tmp_path / "cache"
    native = NativeFixture()
    _, receipt = fingerprint.dataset_receipt(cache, "terminal-bench/terminal-bench", native)
    stdout = cache / receipt["path"]
    target = {
        "stdout": stdout,
        "receipt": cache / receipt["receipt_path"],
        "parent": stdout.parent,
    }[artifact_kind]
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = outside / target.name
    target.rename(destination)
    target.symlink_to(destination, target_is_directory=destination.is_dir())
    original_open = Path.open
    escaped_reads = []

    def contained_open(path, *args, **kwargs):
        if path.resolve().is_relative_to(outside):
            escaped_reads.append(path)
            raise AssertionError("metadata cache attempted to read an outside-root symlink target")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", contained_open)
    with pytest.raises(fingerprint.IntegrityError, match="Symlink target is forbidden"):
        fingerprint.dataset_receipt(cache, "terminal-bench/terminal-bench", native)
    assert escaped_reads == [] and native.calls == 1
    assert target.is_symlink()


def test_matching_digest_cannot_be_applied_to_a_different_task_name(
    fingerprint, tmp_path, monkeypatch,
):
    spec = specification(fingerprint, task="terminal-bench/not-sample")
    monkeypatch.setattr(
        fingerprint, "download_package", lambda *args: pytest.fail("Wrong task identity"),
    )
    result = fingerprint.qualify(
        tmp_path, tmp_path / "upstream", [spec], metadata_for(spec), {}, {}, object(),
    )
    entry = result["packages"][0]
    assert entry["status"] == "unmeasured" and entry["tests_equal"] is None
    assert entry["canonical_dataset_eligible"] is False


def test_equal_exported_test_bytes_do_not_imply_native_digest_coverage(fingerprint, tmp_path):
    original = task_tree(tmp_path / "original")
    hidden = original / "tests/.ignored-support"
    hidden.write_bytes(b"retained but excluded by native hash\n")
    files = fingerprint.inventory(original)
    selected = [
        path for path, info in files.items()
        if info["kind"] != "directory" and path != "tests/.ignored-support"
    ]
    assert fingerprint.compare_inventories(files, files)["categories"]["tests"]["equal"] is True
    coverage = fingerprint.content_hash_coverage(files, files, selected)
    assert coverage["instruction"]["all_exported_content_paths_hashed"] is True
    assert coverage["instruction"]["full_compared_content_paths_digest_covered"] is True
    assert coverage["tests"]["all_exported_content_paths_hashed"] is False
    assert coverage["tests"]["full_compared_content_paths_digest_covered"] is False
    assert coverage["tests"]["exported_content_paths_not_hashed"] == ["tests/.ignored-support"]


def test_hashing_all_exported_tests_does_not_cover_missing_upstream_support(
    fingerprint, tmp_path,
):
    original = task_tree(tmp_path / "original")
    upstream = task_tree(tmp_path / "upstream")
    (upstream / "tests/.missing-support").write_bytes(b"missing from public export\n")
    exported = fingerprint.inventory(original)
    selected = [path for path, info in exported.items() if info["kind"] != "directory"]
    coverage = fingerprint.content_hash_coverage(exported, fingerprint.inventory(upstream), selected)
    assert coverage["tests"]["all_exported_content_paths_hashed"] is True
    assert coverage["tests"]["full_compared_content_paths_digest_covered"] is False
    assert coverage["tests"]["upstream_content_paths_missing_from_export"] == [
        "tests/.missing-support",
    ]


def test_unknown_native_scope_and_absent_dimensions_remain_unknown(fingerprint, tmp_path):
    original = task_tree(tmp_path / "original")
    files = fingerprint.inventory(original)
    unknown = fingerprint.content_hash_coverage(files, files, None)
    assert unknown["instruction"]["all_exported_content_paths_hashed"] is None
    assert unknown["tests"]["full_compared_content_paths_digest_covered"] is None
    absent = fingerprint.content_hash_coverage({}, {}, [])
    assert all(value["full_compared_content_paths_digest_covered"] is None for value in absent.values())


def test_configuration_field_changes_preserve_types_presence_and_pointer_escaping(fingerprint):
    changes = fingerprint.field_changes(
        {"enabled": True, "a/b": {"~key": None}},
        {"enabled": 1, "a/b": {}},
    )
    assert [change["path"] for change in changes] == ["/a~1b/~0key", "/enabled"]
    assert changes[0]["before_present"] is True and changes[0]["after_present"] is False
    assert changes[0]["before_type"] == "NoneType"
    assert changes[1]["before_type"] == "bool"
    assert changes[1]["after_type"] == "int"


def test_configuration_syntax_classification_never_weakens_strict_byte_inequality(
    fingerprint, tmp_path,
):
    upstream = task_tree(tmp_path / "upstream")
    package = task_tree(tmp_path / "package")
    (package / "task.toml").write_bytes(b'# preserved raw-only difference\nversion = "1.0"\n')
    comparison = fingerprint.compare_inventories(
        fingerprint.inventory(package), fingerprint.inventory(upstream),
    )
    assert comparison["categories"]["config"]["equal"] is False
    before = fingerprint.tomllib.loads((upstream / "task.toml").read_text())
    after = fingerprint.tomllib.loads((package / "task.toml").read_text())
    assert fingerprint.configuration_classification(
        False, fingerprint.field_changes(before, after), [], [], [],
    ) == "raw_syntax_or_comment_change_sdk_declarations_equal"


def test_unknown_raw_configuration_fields_are_retained_but_metadata_dict_keys_are_known(
    fingerprint,
):
    class PackageModel:
        model_fields = {"name": SimpleNamespace(alias=None)}
        name = "terminal-bench/sample"

    class TaskModel:
        model_fields = {
            "task": SimpleNamespace(alias=None),
            "metadata": SimpleNamespace(alias=None),
        }
        task = PackageModel()
        metadata = {"arbitrary_task_key": True}

    unknown = fingerprint.unknown_configuration_fields({
        "task": {"name": "terminal-bench/sample", "future_option": {"value": 3}},
        "metadata": {"arbitrary_task_key": True},
        "unknown_top_level": False,
    }, TaskModel())
    assert unknown == {
        "/task/future_option": {"value": {"value": 3}, "type": "dict"},
        "/unknown_top_level": {"value": False, "type": "bool"},
    }
    changes = fingerprint.field_changes({}, unknown)
    assert fingerprint.configuration_classification(
        False, changes, [], [], changes,
    ) == "raw_unknown_fields_changed_sdk_declarations_equal"


def test_unknown_raw_fields_never_silently_truncate_unmapped_sdk_list_items(fingerprint):
    class TaskModel:
        model_fields = {"entries": SimpleNamespace(alias=None)}
        entries = []

    with pytest.raises(ValueError, match="zip"):
        fingerprint.unknown_configuration_fields({
            "entries": [{"future_option": True}],
        }, TaskModel())


def test_configuration_package_identity_changes_are_not_resource_changes(fingerprint):
    changes = fingerprint.field_changes(
        {"task": {"name": "terminal-bench/sample"}},
        {"task": {"name": "other-org/no-prebuild-sample"}},
    )
    assert fingerprint.configuration_classification(
        False, changes, changes, [], [],
    ) == "sdk_package_metadata_only"
    assert fingerprint.configuration_dimensions(changes) == []


def test_configuration_resource_and_prebuild_declaration_changes_remain_distinct(fingerprint):
    before = {"agent": {"environment": {"cpus": 2}}, "verifier": {"environment": {"docker_image": None}}}
    after = {"agent": {"environment": {"cpus": 4}}, "verifier": {"environment": {"docker_image": "public/image@sha256:recorded"}}}
    changes = fingerprint.field_changes(before, after)
    assert fingerprint.configuration_classification(
        False, changes, changes, changes, [],
    ) == "sdk_interpreted_runtime_declaration_changes"
    assert fingerprint.configuration_dimensions(changes) == [
        "cpu", "grader_configuration", "images_prebuild",
    ]


def test_frozen_in1_inventory_hash_uses_component_order_and_ignores_directories(fingerprint):
    records = {
        "a.x": {"kind": "file", "sha256": "b" * 64},
        "a/x": {"kind": "file", "sha256": "a" * 64},
        "empty": {"kind": "directory"},
    }
    expected = fingerprint.sha256(
        (f"a/x\0{'a' * 64}\na.x\0{'b' * 64}\n").encode(),
    )
    flat_order = fingerprint.sha256(
        (f"a.x\0{'b' * 64}\na/x\0{'a' * 64}\n").encode(),
    )
    assert fingerprint.in1_full_inventory_hash(records) == expected
    assert expected != flat_order


@pytest.mark.usefixtures("native_task_config")
def test_sdk_declared_verifier_environment_copy_does_not_invent_resource_changes(fingerprint):
    inherited = (
        b'[environment]\ncpus = 2\nmemory_mb = 4096\nstorage_mb = 10240\ngpus = 0\n'
        b'[verifier]\nenvironment_mode = "separate"\n'
    )
    explicit = inherited + (
        b'[verifier.environment]\ncpus = 2\nmemory_mb = 4096\nstorage_mb = 10240\ngpus = 0\n'
        b'docker_image = "public/verifier@sha256:recorded"\n'
    )
    before, after = fingerprint.parse_configuration(inherited), fingerprint.parse_configuration(explicit)
    assert before["error"] is None and after["error"] is None
    changes = fingerprint.field_changes(
        before["inherited_declaration_projection"], after["inherited_declaration_projection"],
    )
    assert [change["path"] for change in changes] == ["/verifier/environment/docker_image"]
    assert fingerprint.configuration_dimensions(changes) == [
        "grader_configuration", "images_prebuild",
    ]
    assert before["verifier_environment_origin"] == "top_level_environment"
    assert after["verifier_environment_origin"] == "explicit_verifier_environment"


@pytest.mark.usefixtures("native_task_config")
def test_sdk_legacy_units_are_normalization_not_changed_resource_declarations(fingerprint):
    before = fingerprint.parse_configuration(b'[environment]\nmemory = "4G"\n')
    after = fingerprint.parse_configuration(b'[environment]\nmemory_mb = 4096\n')
    assert before["error"] is None and after["error"] is None
    assert before["warnings"]
    assert before["sdk_declarations"] == after["sdk_declarations"]
    assert before["inherited_declaration_projection"] == after["inherited_declaration_projection"]
    assert before["unknown_raw_fields"] == after["unknown_raw_fields"] == {}


@pytest.mark.usefixtures("native_task_config")
def test_invalid_toml_is_unmeasured_not_an_empty_default_configuration(fingerprint):
    original = b"[environment\n"
    parsed = fingerprint.parse_configuration(original)
    assert parsed["error"]["type"] == "TOMLDecodeError"
    assert parsed["raw_sha256"] == fingerprint.sha256(original)
    assert "sdk_declarations" not in parsed
    assert "inherited_declaration_projection" not in parsed
